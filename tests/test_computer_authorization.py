"""Matrix OpenID trust boundary and live Computer policy."""

from pathlib import Path
from unittest.mock import AsyncMock

import aiohttp
import nio
import pytest
from structlog.testing import capture_logs

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.orchestration.computer_runtime import _authorize_computer
from mindroom.worker_computer.auth import MatrixOpenIDToken, verify_openid
from mindroom.worker_computer.sessions import ComputerError
from tests.identity_helpers import entity_ids, persist_entity_accounts

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
    with pytest.raises(ComputerError) as error:
        await verify_openid(token, paths)
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
    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[nio.RoomMember(user, None, None) for user in members],
        room_id="!room:example.org",
    )
    if denial == "pending":
        client.joined_members.return_value = nio.JoinedMembersError("unavailable")
    call = _authorize_computer(
        "@alice:example.org",
        "!room:example.org",
        "@unknown:example.org" if denial == "unknown" else agent_id,
        config=config,
        runtime_paths=paths,
        client=client,
        memberships=AgentReplyMembershipIndex(),
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
    joined = nio.JoinedMembersResponse(
        members=[nio.RoomMember(user, None, None) for user in [agent_id, "@alice:example.org"]],
        room_id="!room:example.org",
    )
    client = AsyncMock(spec=nio.AsyncClient)
    failure = aiohttp.ClientConnectionError("https://matrix.example.org/members?access_token=refresh-secret")
    client.joined_members.return_value = joined
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:example.org"])
    if stage == "rooms":
        client.joined_rooms.side_effect = failure
    else:
        client.joined_members.side_effect = [joined, failure]
    with capture_logs() as logs, pytest.raises(ComputerError) as error:
        await _authorize_computer(
            "@alice:example.org",
            "!room:example.org",
            agent_id,
            config=config,
            runtime_paths=paths,
            client=client,
            memberships=AgentReplyMembershipIndex(),
        )
    assert error.value.status_code == 503
    assert "refresh-secret" not in str(logs)
    assert any(log.get("error") == "ClientConnectionError" for log in logs)
