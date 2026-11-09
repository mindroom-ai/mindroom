"""A plugin route reaches the terminals of an agent its caller may use, those MindRoom's own terminal routes show."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import pytest
from fastapi import APIRouter, Body, Depends, HTTPException

from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.plugin_http import RoomReader, TerminalError, require_room_reader
from tests.api.test_plugin_room_agents import ALICE, BOB, _Room
from tests.terminal.test_routes import _agents, _answer, _auth, _paths, _read_until, _served, _stream

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("enforce_turn_authorization")]


def _plugin_router() -> APIRouter:
    """A plugin's route that opens a terminal in the agent's workspace and types the command its caller confirmed."""
    router = APIRouter()

    @router.post("/api/rooms/{room_id}/agents/{agent_user_id}/run")
    async def run(
        room: Annotated[RoomReader, Depends(require_room_reader)],
        agent_user_id: str,
        command: Annotated[str, Body(embed=True)],
    ) -> dict[str, str]:
        workspace = await room.agent(agent_user_id)
        if workspace.terminal != "type":
            raise HTTPException(403, "You may not type in this agent's terminals.")
        terminals = await room.terminals(workspace, create=True)
        if terminals is None:
            raise HTTPException(403, "This agent has no terminals.")
        try:
            info = await terminals.open()
            async with terminals.attach(info.id) as terminal:
                await terminal.send_input(f"{command}\r")
        except TerminalError as error:
            raise HTTPException(error.status_code, error.detail) from None
        return {"id": info.id}

    @router.get("/api/rooms/{room_id}/agents/{agent_user_id}/has-terminals")
    async def has_terminals(
        room: Annotated[RoomReader, Depends(require_room_reader)],
        agent_user_id: str,
        create: bool = False,
    ) -> dict[str, bool]:
        return {"terminals": await room.terminals(await room.agent(agent_user_id), create=create) is not None}

    return router


@pytest.mark.parametrize("private", [False, True], ids=["shared", "private"])
async def test_a_plugin_route_types_in_the_terminal_mindroom_s_routes_show_its_caller(
    tmp_path: Path,
    runner: str,
    *,
    private: bool,
) -> None:
    """Alice's command runs in a terminal of the workspace MindRoom's Terminal tab shows her; Bob, read-only, is refused.

    Bob sees Alice's terminal in a shared agent's workspace, and none in a private one's, where his workspace is his own.
    """
    agents = _agents(ALICE, BOB)
    if private:
        agents["coder"] = agents["coder"].model_copy(update={"private": AgentPrivateConfig(per="user")})
    room = _Room(Config(agents=agents), _paths(tmp_path, runner), ("coder",))
    room.app.include_router(_plugin_router())
    async with _served(room) as (client, base):
        run = f"{base}/agents/{room.ids['coder']}/run"
        refused = await _answer(client.post(run, json={"command": "echo bob"}, headers=_auth(room, BOB)))
        assert refused == (403, {"detail": "You may not type in this agent's terminals."})
        status, opened = await _answer(
            client.post(run, json={"command": "echo from-$((2*3))-plugin"}, headers=_auth(room, ALICE)),
        )
        assert status == 200

        _, alice_sees = await _answer(client.get(f"{base}/terminals", headers=_auth(room, ALICE)))
        _, bob_sees = await _answer(client.get(f"{base}/terminals", headers=_auth(room, BOB)))
        assert [(t["id"], t["writable"]) for t in alice_sees["terminals"]] == [(opened["id"], True)]
        assert [(t["id"], t["writable"]) for t in bob_sees["terminals"]] == ([] if private else [(opened["id"], False)])
        stream = await _stream(client, room, f"{base}/agents/{room.ids['coder']}/terminals/{opened['id']}", ALICE)
        assert await stream.receive_json() == {"type": "writable", "writable": True}
        await _read_until(stream, b"from-6-plugin")
        await stream.close()


@pytest.mark.parametrize(
    ("agent", "env"),
    [
        pytest.param({"terminals": "type"}, {"MINDROOM_SANDBOX_PROXY_TOOLS": "python"}, id="shell-in-primary"),
        pytest.param({}, {}, id="terminals-unset"),
    ],
)
async def test_an_agent_without_terminals_for_its_caller_has_none(
    tmp_path: Path,
    runner: str,
    agent: dict[str, str],
    env: dict[str, str],
) -> None:
    """A shell in the primary process runs no terminal, and an agent whose config allows none gives none."""
    paths = _paths(tmp_path, runner, **env)
    access = ResponderAccessConfig(users=[ALICE])
    room = _Room(
        Config(agents={"coder": AgentConfig(display_name="Coder", tools=["shell"], access=access, **agent)}),
        paths,
        ("coder",),
    )
    room.app.include_router(_plugin_router())
    async with _served(room) as (client, base):
        url = f"{base}/agents/{room.ids['coder']}/has-terminals"
        assert await _answer(client.get(url, headers=_auth(room, ALICE))) == (200, {"terminals": False})


async def test_terminals_make_the_caller_s_workspace_only_when_asked(tmp_path: Path, runner: str) -> None:
    """Alice's own workspace of a private agent appears only once a route asks for her terminals with ``create``."""
    agents = _agents(ALICE)
    agents["coder"] = agents["coder"].model_copy(update={"private": AgentPrivateConfig(per="user")})
    room = _Room(Config(agents=agents), _paths(tmp_path, runner), ("coder",))
    room.app.include_router(_plugin_router())
    (workspace,) = await room.agents(ALICE)
    async with _served(room) as (client, base):
        url = f"{base}/agents/{room.ids['coder']}/has-terminals"
        assert await _answer(client.get(url, headers=_auth(room, ALICE))) == (200, {"terminals": True})
        assert not workspace.root.exists()
        assert await _answer(client.get(f"{url}?create=true", headers=_auth(room, ALICE))) == (200, {"terminals": True})
        assert workspace.root.is_dir()
