"""Plugin routes can require live room membership, read through a running bot in the room, the router first.

The embedded API binds the reader as it serves, and entity restarts and reloads leave it bound.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from fastapi import FastAPI, HTTPException, Request

from mindroom.api import main
from mindroom.api.app_state import ensure_app_state
from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME, RuntimePaths, resolve_runtime_paths
from mindroom.orchestration.config_updates import build_config_update_plan
from mindroom.orchestration.entity_fleet import EntityStartResults
from mindroom.orchestration.plugin_http_runtime import bind_room_members, unbind_room_members
from mindroom.orchestrator import _MultiAgentOrchestrator, _run_api_server
from mindroom.plugin_http import require_room_member
from tests.conftest import install_bots

ROOM = "!room:example.org"
OTHER_ROOM = "!other:example.org"


@pytest.fixture
def main_app() -> Iterator[FastAPI]:
    """The global API app, with whatever room-members reader it had put back afterwards, even after a failure."""
    state = ensure_app_state(main.app)
    previous = state.room_members
    try:
        yield main.app
    finally:
        state.room_members = previous


def _request(app: FastAPI) -> Request:
    return Request({"type": "http", "app": app, "headers": []})


def _client(*user_ids: str, rooms: tuple[str, ...] = (ROOM,)) -> SimpleNamespace:
    """A Matrix client joined to ``rooms`` that answers ``user_ids`` as the members of ``ROOM``."""
    members = [nio.RoomMember(user_id, None, None) for user_id in user_ids]
    return SimpleNamespace(
        rooms={room_id: object() for room_id in rooms},
        joined_members=AsyncMock(return_value=nio.JoinedMembersResponse(members, ROOM)),
    )


def _bot(client: SimpleNamespace | None, *, running: bool = True) -> SimpleNamespace:
    return SimpleNamespace(client=client, running=running)


def _bound(client: SimpleNamespace) -> FastAPI:
    """An app whose reader asks only a running router bot with ``client``."""
    app = FastAPI()
    bind_room_members(app, {"router": _bot(client)})  # type: ignore[dict-item]
    return app


@pytest.mark.asyncio
async def test_member_passes() -> None:
    """A joined user passes, and the router client is asked about that room."""
    client = _client("@alice:example.org", "@bob:example.org")
    await require_room_member(_request(_bound(client)), ROOM, "@alice:example.org")
    client.joined_members.assert_awaited_once_with(ROOM)


@pytest.mark.asyncio
async def test_non_member_403() -> None:
    """A user who is not joined gets 403."""
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(_bound(_client("@bob:example.org"))), ROOM, "@alice:example.org")
    assert (caught.value.status_code, caught.value.detail) == (403, "Not a member of this room.")


@pytest.mark.asyncio
async def test_unbound_503() -> None:
    """Without a bound reader the answer is 503."""
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(FastAPI()), ROOM, "@alice:example.org")
    assert (caught.value.status_code, caught.value.detail) == (503, "Room membership is unavailable.")


@pytest.mark.asyncio
async def test_joined_members_error_503() -> None:
    """A non-JoinedMembersResponse from the client is 503, never a pass."""
    client = _client()
    client.joined_members = AsyncMock(return_value=nio.JoinedMembersError("boom"))
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(_bound(client)), ROOM, "@alice:example.org")
    assert (caught.value.status_code, caught.value.detail) == (503, "Room membership is unavailable.")


@pytest.mark.asyncio
async def test_joined_members_raising_503() -> None:
    """A Matrix call that raises is 503 like an error response, never a pass and never a bare 500."""
    client = _client()
    client.joined_members = AsyncMock(side_effect=TimeoutError("homeserver timed out"))
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(_bound(client)), ROOM, "@alice:example.org")
    assert (caught.value.status_code, caught.value.detail) == (503, "Room membership is unavailable.")


@pytest.mark.asyncio
async def test_router_in_room_is_asked_first() -> None:
    """The router answers when it is in the room, even when an agent bot is in it too."""
    router = _client("@alice:example.org")
    agent = _client("@alice:example.org")
    app = FastAPI()
    bind_room_members(app, {"general": _bot(agent), "router": _bot(router)})  # type: ignore[dict-item]
    await require_room_member(_request(app), ROOM, "@alice:example.org")
    router.joined_members.assert_awaited_once_with(ROOM)
    agent.joined_members.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_bot_in_room_answers_without_the_router() -> None:
    """In a room the router is not in, such as a personal room or a DM, a running agent bot in it answers."""
    router = _client("@alice:example.org", rooms=(OTHER_ROOM,))
    stopped = _client("@alice:example.org")
    agent = _client("@alice:example.org")
    app = FastAPI()
    bots = {"router": _bot(router), "stopped": _bot(stopped, running=False), "assistant": _bot(agent)}
    bind_room_members(app, bots)  # type: ignore[arg-type]
    await require_room_member(_request(app), ROOM, "@alice:example.org")
    agent.joined_members.assert_awaited_once_with(ROOM)
    router.joined_members.assert_not_awaited()
    stopped.joined_members.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_bot_answers_while_the_router_is_down() -> None:
    """A stopped router, or one without a client, leaves the room to a running agent bot in it."""
    agent = _client("@alice:example.org")
    app = FastAPI()
    bots = {"router": _bot(_client("@alice:example.org"), running=False), "assistant": _bot(agent)}
    bind_room_members(app, bots)  # type: ignore[arg-type]
    await require_room_member(_request(app), ROOM, "@alice:example.org")
    bots["router"] = _bot(None)
    await require_room_member(_request(app), ROOM, "@alice:example.org")
    assert agent.joined_members.await_count == 2


@pytest.mark.asyncio
async def test_no_bot_in_room_503() -> None:
    """When no running bot is in the room the answer is 503, and no bot is asked about a room it is not in."""
    router = _client("@alice:example.org", rooms=(OTHER_ROOM,))
    agent = _client("@alice:example.org", rooms=())
    app = FastAPI()
    bind_room_members(app, {"router": _bot(router), "assistant": _bot(agent)})  # type: ignore[dict-item]
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(app), ROOM, "@alice:example.org")
    assert (caught.value.status_code, caught.value.detail) == (503, "Room membership is unavailable.")
    router.joined_members.assert_not_awaited()
    agent.joined_members.assert_not_awaited()


@pytest.mark.asyncio
async def test_stopped_bots_503() -> None:
    """A bot that stopped after binding is not asked, and the reader stays bound for when one runs again."""
    router = _bot(_client("@alice:example.org"))
    app = FastAPI()
    bind_room_members(app, {"router": router})  # type: ignore[dict-item]
    await require_room_member(_request(app), ROOM, "@alice:example.org")
    router.running = False
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(app), ROOM, "@alice:example.org")
    assert caught.value.status_code == 503
    router.running = True
    await require_room_member(_request(app), ROOM, "@alice:example.org")
    assert router.client.joined_members.await_count == 2


@pytest.mark.asyncio
async def test_restarted_bot_uses_its_new_client() -> None:
    """A bot that got a new client in place, or was replaced in the mapping, is read through its new client."""
    app = FastAPI()
    old = _client("@alice:example.org")
    new = _client("@bob:example.org")
    agent = _bot(old)
    bots = {"router": _bot(_client(rooms=())), "assistant": agent}
    bind_room_members(app, bots)  # type: ignore[arg-type]
    agent.client = new
    await require_room_member(_request(app), ROOM, "@bob:example.org")
    new.joined_members.assert_awaited_once_with(ROOM)
    old.joined_members.assert_not_awaited()
    replacement = _client("@carol:example.org")
    bots["assistant"] = _bot(replacement)
    await require_room_member(_request(app), ROOM, "@carol:example.org")
    replacement.joined_members.assert_awaited_once_with(ROOM)
    bots["assistant"].client = None
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(app), ROOM, "@carol:example.org")
    assert caught.value.status_code == 503


def test_bind_then_unbind_clears() -> None:
    """Unbinding removes the reader."""
    app = _bound(_client())
    assert ensure_app_state(app).room_members is not None
    unbind_room_members(app)
    assert ensure_app_state(app).room_members is None


def _orchestrator_config(tmp_path: Path, *, role: str = "Write code") -> Config:
    """A config with an agent ``code`` that uses the MCP server ``demo``, so a catalog change restarts it."""
    return Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "mcp_servers": {"demo": {"transport": "stdio", "command": "npx"}},
            "agents": {"code": {"display_name": "Code", "role": role, "tools": ["mcp_demo"]}},
        },
        _runtime_paths(tmp_path),
    )


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})


def _running_bot(client: SimpleNamespace) -> MagicMock:
    """A running orchestrator bot with ``client``, whose lifecycle methods do nothing."""
    return MagicMock(spec=AgentBot, running=True, client=client)


async def _membership_status(app: FastAPI) -> int:
    """Return 200 when Alice passes the membership check for ``ROOM``, else the status it answers."""
    try:
        await require_room_member(_request(app), ROOM, "@alice:example.org")
    except HTTPException as error:
        return error.status_code
    return 200


async def _serve_api(orchestrator: _MultiAgentOrchestrator, while_serving: Callable[[], Awaitable[None]]) -> None:
    """Run the embedded API server for ``orchestrator``, doing ``while_serving`` instead of answering requests."""

    class Server:
        should_exit = True
        force_exit = False
        received_signal_name = None

        def __init__(self, _config: object, _shutdown_requested: object, **_kwargs: object) -> None:
            pass

        async def serve(self) -> None:
            await while_serving()

    shutdown_requested = asyncio.Event()
    shutdown_requested.set()
    with (
        patch("mindroom.orchestrator.uvicorn.Config", return_value=object()),
        patch("mindroom.orchestrator._SignalAwareUvicornServer", Server),
    ):
        await _run_api_server("127.0.0.1", 8765, "INFO", orchestrator, shutdown_requested)


@pytest.mark.asyncio
async def test_serving_api_binds_membership_and_a_failed_reload_leaves_it_bound(
    tmp_path: Path,
    main_app: FastAPI,
) -> None:
    """The API binds the reader as it starts serving and withdraws it as it stops; a reload never withdraws it.

    The reload fails after it stopped what changes, which once left the reader withdrawn until the next reload.
    """
    current = _orchestrator_config(tmp_path)
    new = _orchestrator_config(tmp_path, role="Write better code")
    orchestrator = _MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path), config=current)
    orchestrator.running = True
    install_bots(
        orchestrator,
        {ROUTER_AGENT_NAME: _running_bot(_client("@alice:example.org")), "code": _running_bot(_client())},
    )
    plan = build_config_update_plan(current_config=current, new_config=new, bots=orchestrator.agent_bots)
    statuses: list[int] = []

    async def while_serving() -> None:
        statuses.append(await _membership_status(main_app))
        with (
            patch("mindroom.orchestrator.prepare_managed_accounts", new=AsyncMock()),
            patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())),
            patch.object(orchestrator, "_refresh_running_bots_presence", side_effect=RuntimeError("presence failed")),
            pytest.raises(RuntimeError, match="presence failed"),
        ):
            await orchestrator._apply_config_update_plan(current, plan)
        statuses.append(await _membership_status(main_app))

    await _serve_api(orchestrator, while_serving)

    assert statuses == [200, 200]
    assert ensure_app_state(main_app).room_members is None


@pytest.mark.asyncio
async def test_restarting_one_bot_keeps_membership_answering_through_another(
    tmp_path: Path,
    main_app: FastAPI,
) -> None:
    """While a catalog change restarts one bot, the running router in the room keeps answering membership."""
    orchestrator = _MultiAgentOrchestrator(
        runtime_paths=_runtime_paths(tmp_path),
        config=_orchestrator_config(tmp_path),
    )
    orchestrator.running = True
    code = _running_bot(_client("@alice:example.org"))
    install_bots(orchestrator, {ROUTER_AGENT_NAME: _running_bot(_client("@alice:example.org")), "code": code})
    bind_room_members(main_app, orchestrator.agent_bots)
    new_code = _running_bot(_client("@alice:example.org"))
    during_restart: list[int] = []

    async def stop_code(**_kwargs: object) -> None:
        code.running = False
        during_restart.append(await _membership_status(main_app))

    code.stop.side_effect = stop_code
    real_stop = orchestrator.fleet.stop

    async def replace(entity_names: set[str]) -> EntityStartResults:
        # The fleet's own stop runs the orchestrator's entities_changing step before the bot stops.
        await real_stop(entity_names)
        install_bots(orchestrator, {"code": new_code})
        return EntityStartResults(started_bots=[new_code])

    with (
        patch.object(orchestrator.fleet, "replace", side_effect=replace),
        patch.object(orchestrator._rooms, "setup", new=AsyncMock()),
    ):
        await orchestrator._handle_mcp_catalog_change("demo")

    assert during_restart == [200]
    assert await _membership_status(main_app) == 200
