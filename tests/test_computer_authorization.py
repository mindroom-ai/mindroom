"""Matrix OpenID trust boundary and live Computer policy."""

from pathlib import Path
from unittest.mock import AsyncMock

import aiohttp
import nio
import pytest
from fastapi import FastAPI
from structlog.testing import capture_logs

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.api import main as api_main
from mindroom.api.app_state import ensure_app_state
from mindroom.api.computers import ComputerRuntime
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME, RuntimePaths, resolve_runtime_paths
from mindroom.matrix_openid import MatrixOpenIDError, MatrixOpenIDToken, verify_matrix_openid
from mindroom.orchestration.computer_runtime import _authorize_computer, _computer_browser_provider
from mindroom.runtime_env_policy import WORKER_COMPUTER_ENABLED_ENV
from mindroom.tool_system.worker_routing import build_agent_toolkit_worker_target, build_tool_execution_identity
from mindroom.worker_computer.sessions import ComputerError
from tests.access_schema_support import joined_members_response
from tests.identity_helpers import entity_ids, persist_entity_accounts
from tests.runtime_fakes import RecordingRouter, ready_runtime

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")
TEST_OPENID_TOKEN = "openid-test"  # noqa: S105 - isolated test credential


@pytest.mark.asyncio
async def test_openid_rejects_untrusted_server_before_network(tmp_path: Path) -> None:
    """Openid rejects untrusted server before network."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    token = MatrixOpenIDToken(
        access_token=TEST_OPENID_TOKEN,
        token_type="Bearer",  # noqa: S106 - isolated test token
        matrix_server_name="attacker.invalid",
        expires_in=30,
    )
    with pytest.raises(MatrixOpenIDError) as error:
        await verify_matrix_openid(token, paths)
    assert error.value.status_code == 401


@pytest.mark.parametrize("expires_in", [0, -1])
def test_expired_openid_input_is_rejected(expires_in: int) -> None:
    """Expired openid input is rejected."""
    with pytest.raises(ValueError, match="greater than 0"):
        MatrixOpenIDToken(
            access_token=TEST_OPENID_TOKEN,
            token_type="Bearer",  # noqa: S106 - isolated test token
            matrix_server_name="example.org",
            expires_in=expires_in,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize(
    "denial",
    ["requester", "agent", "policy", "scope", "routing", "unknown", "pending", "backend", None],
)
async def test_live_membership_policy_and_canonical_browser_scope(
    tmp_path: Path,
    denial: str | None,
    *,
    private: bool,
) -> None:
    """Live membership policy and canonical browser scope."""
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MATRIX_HOMESERVER": "https://example.org",
            "MINDROOM_WORKER_BACKEND": {"backend": "static"}.get(str(denial), "docker"),
        },
    )
    config = Config(
        agents={
            "writer": AgentConfig(
                display_name="Writer",
                tools=["browser"],
                worker_tools=["browser"],
                worker_scope="user_agent",
                access=ResponderAccessConfig(users=["@alice:example.org"]),
            ),
        },
    )
    if private:
        config.agents["writer"].worker_scope = None
        config.agents["writer"].private = AgentPrivateConfig(per="user_agent")
    persist_entity_accounts(config, paths)
    agent_id = entity_ids(config, paths)["writer"].full_id
    members = {"@alice:example.org", agent_id}
    if denial == "requester":
        members.remove("@alice:example.org")
    if denial == "agent":
        members.remove(agent_id)
    if denial == "policy":
        config.agents["writer"].access = ResponderAccessConfig(
            users=[],
            current_room_members=False,
            members_of_rooms=[],
        )
    if denial == "scope":
        if private:
            config.agents["writer"].private = AgentPrivateConfig(per="user")
        else:
            config.agents["writer"].worker_scope = "user"
    if denial == "routing":
        config.agents["writer"].worker_tools = []
    client = AsyncMock(spec=nio.AsyncClient)
    client.joined_members.return_value = joined_members_response("!room:example.org", *members)
    if denial == "pending":
        client.joined_members.return_value = nio.JoinedMembersError("unavailable")
    memberships = AgentReplyMembershipIndex()
    call = _authorize_computer(
        "@alice:example.org",
        "!room:example.org",
        "@unknown:example.org" if denial == "unknown" else agent_id,
        config=config,
        runtime_paths=paths,
        client=client,
        memberships=memberships,
        refresh_memberships=lambda: memberships.refresh(config, paths, client),
    )
    if denial:
        with pytest.raises(ComputerError) as error:
            await call
        assert error.value.status_code == (
            409 if denial == "scope" else 503 if denial in {"routing", "pending", "backend"} else 403
        )
    else:
        target = await call
        assert target.spec.worker_key == "v1:default:user_agent:~@alice:example.org:writer"
        assert target.spec.private_agent_names == (frozenset({"writer"}) if private else frozenset())


@pytest.mark.asyncio
async def test_a_member_list_raced_by_a_membership_change_is_read_again(tmp_path: Path) -> None:
    """A joiner's first Computer call, whose member read the client refuses for that join, reads again and passes."""
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MATRIX_HOMESERVER": "https://example.org", "MINDROOM_WORKER_BACKEND": "docker"},
    )
    config = Config(
        agents={
            "writer": AgentConfig(
                display_name="Writer",
                tools=["browser"],
                worker_tools=["browser"],
                worker_scope="user_agent",
                access=ResponderAccessConfig(users=["@alice:example.org"]),
            ),
        },
    )
    persist_entity_accounts(config, paths)
    agent_id = entity_ids(config, paths)["writer"].full_id
    client = AsyncMock(spec=nio.AsyncClient)
    client.joined_members.side_effect = [
        nio.LocalProtocolError("room membership changed during lookup"),
        joined_members_response("!room:example.org", "@alice:example.org", agent_id),
    ]
    memberships = AgentReplyMembershipIndex()
    target = await _authorize_computer(
        "@alice:example.org",
        "!room:example.org",
        agent_id,
        config=config,
        runtime_paths=paths,
        client=client,
        memberships=memberships,
        refresh_memberships=AsyncMock(),
    )
    assert target.spec.worker_key == "v1:default:user_agent:~@alice:example.org:writer"
    assert client.joined_members.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["rooms", "members"])
async def test_actual_membership_refresh_does_not_log_transport_urls(tmp_path: Path, stage: str) -> None:
    """The shared grant refresh retains uncertainty while removing URL-bearing diagnostics."""
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MATRIX_HOMESERVER": "https://example.org", "MINDROOM_WORKER_BACKEND": "docker"},
    )
    config = Config(
        agents={
            "writer": AgentConfig(
                display_name="Writer",
                tools=["browser"],
                worker_tools=["browser"],
                worker_scope="user_agent",
                access=ResponderAccessConfig(current_room_members=True),
            ),
        },
    )
    persist_entity_accounts(config, paths)
    agent_id = entity_ids(config, paths)["writer"].full_id
    joined = joined_members_response("!room:example.org", agent_id, "@alice:example.org")
    client = AsyncMock(spec=nio.AsyncClient)
    failure = aiohttp.ClientConnectionError("https://matrix.example.org/members?access_token=refresh-secret")
    client.joined_members.return_value = joined
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:example.org"])
    if stage == "rooms":
        client.joined_rooms.side_effect = failure
    else:
        client.joined_members.side_effect = [joined, failure]
    memberships = AgentReplyMembershipIndex()
    with capture_logs() as logs, pytest.raises(ComputerError) as error:
        await _authorize_computer(
            "@alice:example.org",
            "!room:example.org",
            agent_id,
            config=config,
            runtime_paths=paths,
            client=client,
            memberships=memberships,
            refresh_memberships=lambda: memberships.refresh(config, paths, client),
        )
    assert error.value.status_code == 503
    assert "refresh-secret" not in str(logs)
    assert any(log.get("error") == "ClientConnectionError" for log in logs)


@pytest.mark.parametrize("tools", [["browser_mcp"], ["browser", "browser_mcp"]])
def test_computer_selects_exactly_one_browser_provider(tmp_path: Path, tools: list[str]) -> None:
    """Native worker tools are eligible; ambiguous browser providers are refused."""
    from mindroom.orchestration.computer_runtime import _resolve_target  # noqa: PLC0415

    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MINDROOM_WORKER_BACKEND": "docker"},
    )
    config = Config(
        agents={
            "writer": AgentConfig(display_name="Writer", tools=tools, worker_tools=tools, worker_scope="user_agent"),
        },
    )
    if len(tools) == 2:
        with pytest.raises(ComputerError, match="exactly one"):
            _resolve_target("@alice:example.org", "!room:example.org", "@writer:example.org", "writer", config, paths)
    else:
        target = _resolve_target(
            "@alice:example.org",
            "!room:example.org",
            "@writer:example.org",
            "writer",
            config,
            paths,
        )
        assert "user_agent" in target.spec.worker_key


@pytest.mark.parametrize(
    ("backend", "tools", "worker_tools", "provider", "status"),
    [
        ("docker", ["browser"], ["browser"], "browser", None),
        ("kubernetes", ["browser_mcp"], ["browser_mcp"], "browser_mcp", None),
        ("static", ["browser"], ["browser"], None, 503),
        ("docker", ["browser", "browser_mcp"], ["browser", "browser_mcp"], None, 409),
        ("docker", ["shell"], ["shell"], None, 409),
        ("docker", ["browser"], [], None, 503),
    ],
)
def test_computer_browser_provider_matches_api_checks(
    tmp_path: Path,
    backend: str,
    tools: list[str],
    worker_tools: list[str],
    provider: str | None,
    status: int | None,
) -> None:
    """The shared worker computer check returns the provider or raises the API's exact errors."""
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MINDROOM_WORKER_BACKEND": backend},
    )
    config = Config(
        agents={
            "writer": AgentConfig(
                display_name="Writer",
                tools=tools,
                worker_tools=worker_tools,
                worker_scope="user_agent",
            ),
        },
    )
    if status is None:
        assert _computer_browser_provider("writer", config, paths) == provider
    else:
        with pytest.raises(ComputerError) as error:
            _computer_browser_provider("writer", config, paths)
        assert error.value.status_code == status


ROOM = "!room:example.org"
ALICE = "@alice:example.org"
ALIAS = "@telegram_alice:example.org"


def _bound_computer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    joined_rooms: list[str],
    *,
    unprepared: str | None = None,
) -> tuple[ComputerRuntime, AsyncMock, str, RuntimePaths]:
    """Bind the runtime's Computer authorization with Alice, her bridge alias, and a writer Alice may use in ``ROOM``.

    Return it, the router's client, the writer's user ID, and the runtime's paths. An ``unprepared`` agent is
    configured too, without its account.
    """
    writer = AgentConfig(
        display_name="Writer",
        tools=["browser"],
        worker_tools=["browser"],
        worker_scope="user_agent",
        access=ResponderAccessConfig(users=[ALICE]),
    )
    config = Config(agents={"writer": writer}, authorization={"aliases": {ALICE: [ALIAS]}})
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MATRIX_HOMESERVER": "https://example.org",
            "MINDROOM_WORKER_BACKEND": "docker",
            WORKER_COMPUTER_ENABLED_ENV: "true",
        },
    )
    persist_entity_accounts(config, paths)
    agent_id = entity_ids(config, paths)["writer"].full_id
    if unprepared is not None:
        agents = {"writer": writer, unprepared: AgentConfig(display_name="Unprepared")}
        config = Config(agents=agents, authorization=config.authorization)
    client = AsyncMock(spec=nio.AsyncClient)
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=joined_rooms)
    client.joined_members.return_value = joined_members_response(ROOM, ALICE, ALIAS, agent_id)
    runtime = ready_runtime(config, paths)
    runtime.fleet.add(ROUTER_AGENT_NAME, RecordingRouter(client, config))  # type: ignore[arg-type]
    app = FastAPI()
    api_main.initialize_api_app(app, paths)
    monkeypatch.setattr(api_main, "app", app)
    runtime._computer_runtime.bind_if_ready(config, runtime.agent_bots)
    computer = ensure_app_state(app).computer_runtime
    assert computer is not None
    return computer, client, agent_id, paths


@pytest.mark.asyncio
async def test_computer_checks_read_only_their_room_once_grants_are_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the router in many rooms, the first check refreshes grants once; each later one reads only its room."""
    others = [f"!other{index}:example.org" for index in range(60)]
    computer, client, agent_id, _paths = _bound_computer(tmp_path, monkeypatch, [ROOM, *others])

    await computer.authorize(ALICE, ROOM, agent_id)
    assert (client.joined_rooms.await_count, client.joined_members.await_count) == (1, 1 + 1 + len(others))

    for _ in range(3):
        before = client.joined_members.await_count
        await computer.authorize(ALICE, ROOM, agent_id)
        assert client.joined_members.await_count - before == 1
    assert client.joined_rooms.await_count == 1


@pytest.mark.asyncio
async def test_a_bridge_alias_opens_the_computer_its_person_s_turns_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller signed in as Alice's bridge alias reaches the worker Alice's turns of the agent use, not one of its own."""
    computer, _client, agent_id, paths = _bound_computer(tmp_path, monkeypatch, [ROOM])

    alias = await computer.authorize(ALIAS, ROOM, agent_id)
    person = await computer.authorize(ALICE, ROOM, agent_id)

    turn = build_agent_toolkit_worker_target(
        "user_agent",
        "writer",
        is_private=False,
        execution_identity=build_tool_execution_identity(
            channel="matrix",
            agent_name="writer",
            runtime_paths=paths,
            requester_id=ALICE,
            room_id=ROOM,
            thread_id="$thread",
            resolved_thread_id="$thread",
            session_id="session",
        ),
        runtime_paths=paths,
    )
    assert alias.requester_id == ALIAS
    assert alias.spec.worker_key == person.spec.worker_key == turn.worker_key


@pytest.mark.asyncio
async def test_an_agent_without_its_account_leaves_a_ready_agent_s_computer_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While another agent's account is not prepared, the ready writer's Computer opens; another user ID answers 503."""
    computer, _client, agent_id, _paths = _bound_computer(tmp_path, monkeypatch, [ROOM], unprepared="idle")

    target = await computer.authorize(ALICE, ROOM, agent_id)

    assert target.spec.worker_key == f"v1:default:user_agent:~{ALICE}:writer"
    with pytest.raises(ComputerError) as error:
        await computer.authorize(ALICE, ROOM, "@mindroom_idle:example.org")
    assert (error.value.status_code, error.value.detail) == (503, "Agent identities are not ready.")
