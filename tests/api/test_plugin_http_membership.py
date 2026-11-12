"""Plugin routes can require live room membership, read through a running bot in the room, the router first.

The embedded API binds the reader as it serves, and entity restarts and reloads leave it bound.
"""

import asyncio
import gc
import weakref
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import nio
import pytest
from fastapi import FastAPI, HTTPException, Request

from mindroom.api import main
from mindroom.api.app_state import ensure_app_state
from mindroom.api.client_sessions import client_session_store
from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME, RuntimePaths, resolve_runtime_paths
from mindroom.orchestration import plugin_http_runtime
from mindroom.orchestration.config_updates import build_config_update_plan
from mindroom.orchestration.entity_fleet import EntityStartResults
from mindroom.orchestration.plugin_http_runtime import bind_room_members, unbind_room_members
from mindroom.orchestrator import _MultiAgentOrchestrator, _run_api_server
from mindroom.plugin_http import ClientUser, require_room_member
from tests.access_schema_support import joined_members_response
from tests.conftest import install_bots

ROOM = "!room:example.org"
OTHER_ROOM = "!other:example.org"


@pytest.fixture
def main_app() -> Iterator[FastAPI]:
    """The global API app, with whatever room readers it had put back afterwards, even after a failure."""
    state = ensure_app_state(main.app)
    previous = state.room_members, state.room_state, state.room_direct, state.room_history
    yield main.app
    state.room_members, state.room_state, state.room_direct, state.room_history = previous


def _request(app: FastAPI, user_id: str = "@alice:example.org") -> Request:
    """A request signed in with a client session of ``user_id``."""
    token = client_session_store(app).create(user_id).token
    return Request({"type": "http", "app": app, "headers": [(b"authorization", f"Bearer {token}".encode())]})


def _client(*user_ids: str, rooms: tuple[str, ...] = (ROOM,)) -> SimpleNamespace:
    """A Matrix client joined to ``rooms`` that answers ``user_ids`` as the members of ``ROOM``."""
    return SimpleNamespace(
        rooms={room_id: object() for room_id in rooms},
        joined_members=AsyncMock(return_value=joined_members_response(ROOM, *user_ids)),
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
    """A joined user passes as the session's user, and the router client is asked about that room."""
    client = _client("@alice:example.org", "@bob:example.org")
    assert await require_room_member(_request(_bound(client)), ROOM) == ClientUser("@alice:example.org")
    client.joined_members.assert_awaited_once_with(ROOM)


@pytest.mark.asyncio
async def test_no_session_401_before_membership() -> None:
    """Without a client session the answer is 401, and no bot is asked about the room."""
    client = _client("@alice:example.org")
    with pytest.raises(HTTPException) as caught:
        await require_room_member(Request({"type": "http", "app": _bound(client), "headers": []}), ROOM)
    assert caught.value.status_code == 401
    client.joined_members.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_member_403() -> None:
    """A user who is not joined gets 403."""
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(_bound(_client("@bob:example.org"))), ROOM)
    assert (caught.value.status_code, caught.value.detail) == (403, "Not a member of this room.")


@pytest.mark.asyncio
async def test_unbound_503() -> None:
    """Without a bound reader the answer is 503."""
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(FastAPI()), ROOM)
    assert (caught.value.status_code, caught.value.detail) == (503, "Room membership is unavailable.")


@pytest.mark.asyncio
async def test_joined_members_error_503() -> None:
    """A non-JoinedMembersResponse from the client is 503, never a pass."""
    client = _client()
    client.joined_members = AsyncMock(return_value=nio.JoinedMembersError("boom"))
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(_bound(client)), ROOM)
    assert (caught.value.status_code, caught.value.detail) == (503, "Room membership lookup failed; try again.")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [TimeoutError("homeserver timed out"), nio.LocalProtocolError("durable sync session is closed")],
)
async def test_joined_members_raising_503(error: Exception) -> None:
    """A Matrix call that raises is 503 like an error response, never a pass, never a bare 500, and never read again."""
    client = _client()
    client.joined_members = AsyncMock(side_effect=error)
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(_bound(client)), ROOM)
    assert (caught.value.status_code, caught.value.detail) == (503, "Room membership lookup failed; try again.")
    client.joined_members.assert_awaited_once_with(ROOM)


@pytest.mark.asyncio
@pytest.mark.parametrize(("refusals", "status"), [(1, 200), (2, 200), (3, 503)])
async def test_a_member_list_the_client_refuses_for_a_membership_change_is_read_again(
    refusals: int,
    status: int,
) -> None:
    """A join applied while the member list is read makes the client refuse it; the lookup reads it again, 3 times at most."""
    client = _client("@alice:example.org")
    answer = client.joined_members.return_value
    client.joined_members.side_effect = [
        *(nio.LocalProtocolError("room membership changed during lookup") for _ in range(refusals)),
        answer,
    ]
    assert await _membership_status(_bound(client)) == status
    assert client.joined_members.await_count == min(refusals + 1, 3)


@pytest.mark.asyncio
async def test_joined_members_that_never_answers_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Matrix call that hangs gives up after the lookup timeout with 503, never holding the request open."""
    monkeypatch.setattr(plugin_http_runtime, "_LOOKUP_TIMEOUT_SECONDS", 0.01)
    client = _client()

    async def never_answers(_room_id: str) -> None:
        await asyncio.Event().wait()

    client.joined_members = never_answers
    with pytest.raises(HTTPException) as caught:
        async with asyncio.timeout(5):
            await require_room_member(_request(_bound(client)), ROOM)
    assert (caught.value.status_code, caught.value.detail) == (503, "Room membership lookup failed; try again.")


@pytest.mark.asyncio
async def test_callers_share_a_running_lookup_but_not_a_finished_one() -> None:
    """Callers that ask while a room's lookup runs share it, each answered for their own user; a later one asks again."""
    client = _client("@alice:example.org")
    app = _bound(client)
    users = ("@alice:example.org", "@bob:example.org", "@alice:example.org")
    # All three ask before the lookup the first one starts gets to run.
    callers = [asyncio.create_task(_membership_status(app, user_id)) for user_id in users]
    assert await asyncio.gather(*callers) == [200, 403, 200]
    client.joined_members.assert_awaited_once_with(ROOM)
    client.joined_members.return_value = joined_members_response(ROOM, "@bob:example.org")
    assert await _membership_status(app) == 403
    assert client.joined_members.await_count == 2


@pytest.mark.asyncio
async def test_a_caller_after_a_lookup_answered_gets_a_fresh_answer() -> None:
    """A caller that asks once a lookup answered starts a new one, even before the finished one is cleared away."""
    late: list[asyncio.Task[int]] = []
    release = asyncio.Event()

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        if not late:
            # This caller's first step runs after the lookup ends and before anything that ending scheduled.
            late.append(asyncio.create_task(_membership_status(app)))
            return joined_members_response(ROOM, "@alice:example.org")
        await release.wait()
        return joined_members_response(ROOM)

    client = _client()
    client.joined_members = AsyncMock(side_effect=joined_members)
    app = _bound(client)
    assert await _membership_status(app) == 200
    # Asks while the late caller's lookup runs, so it shares that one.
    later = asyncio.create_task(_membership_status(app))
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(late[0], later) == [403, 403]
    assert client.joined_members.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "status"),
    [(joined_members_response(ROOM, "@alice:example.org"), 200), (nio.JoinedMembersError("boom"), 503)],
)
async def test_a_finished_lookup_is_not_kept(
    answer: nio.JoinedMembersResponse | nio.JoinedMembersError,
    status: int,
) -> None:
    """Once a lookup answered or failed and its caller has the answer, the bound reader holds on to nothing of it."""
    client = _client()
    client.joined_members.return_value = answer
    # Kept for the whole test: the reader, and whatever it holds, lives as long as the app it is bound to.
    app = _bound(client)
    caller = asyncio.create_task(_membership_status(app))
    await asyncio.sleep(0)
    (lookup,) = asyncio.all_tasks() - {caller, asyncio.current_task()}
    finished = weakref.ref(lookup)
    del lookup
    assert await caller == status
    gc.collect()
    assert finished() is None
    assert ensure_app_state(app).room_members is not None


@pytest.mark.asyncio
async def test_callers_for_another_room_get_its_own_lookup() -> None:
    """A lookup answers only for its own room, even while a caller for another room waits at the same time."""
    client = _client(rooms=(ROOM, OTHER_ROOM))
    answers = {
        ROOM: joined_members_response(ROOM, "@alice:example.org"),
        OTHER_ROOM: joined_members_response(OTHER_ROOM),
    }
    client.joined_members.side_effect = answers.__getitem__
    app = _bound(client)
    callers = [asyncio.create_task(_membership_status(app, room_id=room_id)) for room_id in (ROOM, OTHER_ROOM)]
    assert await asyncio.gather(*callers) == [200, 403]
    assert client.joined_members.await_args_list == [call(ROOM), call(OTHER_ROOM)]


@pytest.mark.asyncio
async def test_a_failed_lookup_fails_every_caller_sharing_it_and_is_not_kept() -> None:
    """A lookup that fails answers 503 to every caller sharing it, and the next caller asks again."""
    client = _client()
    client.joined_members.return_value = nio.JoinedMembersError("boom")
    app = _bound(client)
    callers = [asyncio.create_task(_membership_status(app)) for _ in range(3)]
    assert await asyncio.gather(*callers) == [503, 503, 503]
    client.joined_members.return_value = joined_members_response(ROOM, "@alice:example.org")
    assert await _membership_status(app) == 200
    assert client.joined_members.await_count == 2


@pytest.mark.asyncio
async def test_a_caller_that_goes_away_leaves_the_lookup_to_the_others() -> None:
    """Cancelling the caller that started a room's lookup does not cancel it for the callers sharing it."""
    client = _client("@alice:example.org")
    app = _bound(client)
    first, second = (asyncio.create_task(_membership_status(app)) for _ in range(2))
    # After one turn of the loop both have asked, and the lookup the first one started has not run yet.
    await asyncio.sleep(0)
    first.cancel()
    assert await second == 200
    assert first.cancelled()
    client.joined_members.assert_awaited_once_with(ROOM)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_a_lookup_ending_after_its_callers_left_reports_no_error(cancelled: bool) -> None:
    """A lookup that fails or is cancelled once every caller sharing it went away gives asyncio no error to report."""
    reported: list[dict[str, object]] = []
    asyncio.get_running_loop().set_exception_handler(lambda _loop, context: reported.append(context))
    client = _client()
    release = asyncio.Event()

    async def fails_once_released(_room_id: str) -> nio.JoinedMembersError:
        await release.wait()
        return nio.JoinedMembersError("boom")

    client.joined_members = fails_once_released
    caller = asyncio.create_task(_membership_status(_bound(client)))
    await asyncio.sleep(0)
    (lookup,) = asyncio.all_tasks() - {caller, asyncio.current_task()}
    caller.cancel()
    await asyncio.wait([caller])
    if cancelled:
        lookup.cancel()
    else:
        release.set()
    await asyncio.wait([lookup])
    # The cancelled caller's traceback holds the lookup too; asyncio reports an unretrieved exception as it is freed.
    del caller, lookup
    gc.collect()
    assert reported == []


@pytest.mark.asyncio
async def test_router_in_room_is_asked_first() -> None:
    """The router answers when it is in the room, even when an agent bot is in it too."""
    router = _client("@alice:example.org")
    agent = _client("@alice:example.org")
    app = FastAPI()
    bind_room_members(app, {"general": _bot(agent), "router": _bot(router)})  # type: ignore[dict-item]
    await require_room_member(_request(app), ROOM)
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
    await require_room_member(_request(app), ROOM)
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
    await require_room_member(_request(app), ROOM)
    bots["router"] = _bot(None)
    await require_room_member(_request(app), ROOM)
    assert agent.joined_members.await_count == 2


@pytest.mark.asyncio
async def test_no_bot_in_room_503() -> None:
    """When no running bot is in the room the answer is 503, and no bot is asked about a room it is not in."""
    router = _client("@alice:example.org", rooms=(OTHER_ROOM,))
    agent = _client("@alice:example.org", rooms=())
    app = FastAPI()
    bind_room_members(app, {"router": _bot(router), "assistant": _bot(agent)})  # type: ignore[dict-item]
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(app), ROOM)
    assert (caught.value.status_code, caught.value.detail) == (503, "No running MindRoom bot is in this room.")
    router.joined_members.assert_not_awaited()
    agent.joined_members.assert_not_awaited()


@pytest.mark.asyncio
async def test_stopped_bots_503() -> None:
    """A bot that stopped after binding is not asked, and the reader stays bound for when one runs again."""
    router = _bot(_client("@alice:example.org"))
    app = FastAPI()
    bind_room_members(app, {"router": router})  # type: ignore[dict-item]
    await require_room_member(_request(app), ROOM)
    router.running = False
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(app), ROOM)
    assert caught.value.status_code == 503
    router.running = True
    await require_room_member(_request(app), ROOM)
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
    await require_room_member(_request(app, "@bob:example.org"), ROOM)
    new.joined_members.assert_awaited_once_with(ROOM)
    old.joined_members.assert_not_awaited()
    replacement = _client("@carol:example.org")
    bots["assistant"] = _bot(replacement)
    await require_room_member(_request(app, "@carol:example.org"), ROOM)
    replacement.joined_members.assert_awaited_once_with(ROOM)
    bots["assistant"].client = None
    with pytest.raises(HTTPException) as caught:
        await require_room_member(_request(app, "@carol:example.org"), ROOM)
    assert caught.value.status_code == 503


def test_bind_then_unbind_clears() -> None:
    """Unbinding removes every room reader."""
    app = _bound(_client())
    state = ensure_app_state(app)
    assert None not in (state.room_members, state.room_state, state.room_direct, state.room_history)
    unbind_room_members(app)
    assert (state.room_members, state.room_state, state.room_direct, state.room_history) == (None, None, None, None)


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


async def _membership_status(app: FastAPI, user_id: str = "@alice:example.org", room_id: str = ROOM) -> int:
    """Return 200 when ``user_id`` passes the membership check for ``room_id``, else the status it answers."""
    try:
        await require_room_member(_request(app, user_id), room_id)
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
            patch.object(orchestrator, "_refresh_running_bots", side_effect=RuntimeError("presence failed")),
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
