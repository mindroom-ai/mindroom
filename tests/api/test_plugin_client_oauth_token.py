"""A plugin route reads the access token of its caller's own OAuth connection, as the agent's tools use it for them.

It answers only a caller who may use the agent, and only a connection kept for that one person, never a shared or
unscoped agent's, nor a service account's.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import httpx
import nio
import pytest
from fastapi import FastAPI, HTTPException, Request
from structlog.testing import capture_logs

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.api.app_state import ensure_app_state
from mindroom.api.config_lifecycle import ApiSnapshot, ApiState, require_api_state
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME, resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.matrix.state import update_matrix_state
from mindroom.oauth import credential_store
from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialContext,
    load_oauth_credentials_snapshot_sync,
    resolve_oauth_credential_context,
)
from mindroom.oauth.service import lookup_oauth_connect_token
from mindroom.plugin_api import OAuthConnectionRequired, OAuthProviderError
from mindroom.plugin_http import ClientUser, client_user_oauth_access_token
from mindroom.runtime_resolution import resolve_agent_execution
from mindroom.tool_system.plugins import PLUGIN_NAME_SCOPE_KEY, config_plugins
from mindroom.tool_system.worker_routing import build_tool_execution_identity
from tests.access_schema_support import joined_members_response
from tests.conftest import bind_runtime_paths, publish_test_plugins
from tests.oauth_test_utils import (
    corrupt_oauth_credential_payload,
    publish_oauth_credentials,
    rotated_token_response,
    serve_token_endpoint,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.api.app_state import EmbeddedRuntime
    from mindroom.constants import RuntimePaths
    from mindroom.oauth import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

ALICE = "@alice:example.com"
# Alice's account on a bridged network, which the config names as hers.
ALICE_BRIDGED = "@telegram_alice:example.com"
BOB = "@bob:example.com"
# A bridge's bot account, which may converse with agents but is no person.
BRIDGE_BOT = "@bridgebot:example.com"
ROOM = "!room:example.com"
# The managed room `lounge`, whose members the agent `lounge` answers; Alice is joined, Bob is not.
LOUNGE = "!lounge:example.com"
PROVIDER = "acme_drive"
TOKEN_URL = "https://accounts.example.com/oauth/token"  # noqa: S105
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105
ALICE_AND_BOB = {"users": [ALICE, BOB]}
# Alice and Bob may use every agent but `bobs` and `lounge`, which only the lounge's members may use. `team` and
# `lobby` share their connections among everyone who uses them; the others keep each person's apart.
AGENTS = {
    "assistant": {"display_name": "Assistant", "worker_scope": "user_agent", "access": ALICE_AND_BOB},
    "helper": {"display_name": "Helper", "worker_scope": "user", "access": ALICE_AND_BOB},
    "notes": {"display_name": "Notes", "private": {"per": "user_agent"}, "access": ALICE_AND_BOB},
    "bobs": {"display_name": "Bob's", "worker_scope": "user_agent", "access": {"users": [BOB]}},
    "lounge": {"display_name": "Lounge", "worker_scope": "user_agent", "rooms": ["lounge"]},
    "team": {"display_name": "Team", "worker_scope": "shared", "access": ALICE_AND_BOB},
    "lobby": {"display_name": "Lobby", "access": ALICE_AND_BOB},
}
OAUTH_MODULE = """
from mindroom.plugin_api import OAuthProvider


def register_oauth_providers(settings, runtime_paths):
    del settings, runtime_paths
    return [
        OAuthProvider(
            id="acme_drive",
            display_name="Acme Drive",
            authorization_url="https://accounts.example.com/oauth/authorize",
            token_url="https://accounts.example.com/oauth/token",
            scopes=("read",),
            credential_service="acme_drive_oauth",
            client_config_services=("acme_drive_oauth_client",),
        ),
        # Each person's connection serves every agent they use, as GitHub's does.
        OAuthProvider(
            id="acme_code",
            display_name="Acme Code",
            authorization_url="https://code.example.com/oauth/authorize",
            token_url="https://code.example.com/oauth/token",
            scopes=("read",),
            credential_service="acme_code_oauth",
            client_config_services=("acme_code_oauth_client",),
            requester_scoped_credentials=True,
        ),
    ]
"""


@dataclass
class _Runtime:
    """What `mindroom run` lends the API: its running router, and its room grants, which it reads when they need it."""

    config: Config
    runtime_paths: RuntimePaths
    agent_reply_memberships: AgentReplyMembershipIndex = field(default_factory=AgentReplyMembershipIndex)
    room_reads: int = 0
    """How many times the grants were read from the rooms."""
    client: SimpleNamespace = field(
        default_factory=lambda: SimpleNamespace(
            joined_rooms=AsyncMock(return_value=nio.JoinedRoomsResponse([LOUNGE])),
            joined_members=AsyncMock(return_value=joined_members_response(LOUNGE, ALICE)),
        ),
    )

    def agent_bot(self, name: str) -> SimpleNamespace | None:
        if name != ROUTER_AGENT_NAME:
            return None
        return SimpleNamespace(running=True, client=self.client, config=self.config)

    async def refresh_agent_reply_memberships_when_needed(self) -> None:
        if self.agent_reply_memberships.needs_refresh(self.config):
            self.room_reads += 1
            await self.agent_reply_memberships.refresh(
                self.config,
                self.runtime_paths,
                cast("nio.AsyncClient", self.client),
            )


@dataclass(frozen=True)
class _Deployment:
    """A MindRoom whose API serves the plugin ``drive``, which brings the provider ``acme_drive``, and ``AGENTS``."""

    config: Config
    runtime_paths: RuntimePaths
    request: Request
    """A request one of the plugin's routes serves."""
    runtime: _Runtime

    def target(self, agent_name: str, requester_id: str) -> ResolvedWorkerTarget:
        """Return the worker target the agent's tools use in a turn for ``requester_id``."""
        identity = build_tool_execution_identity(
            channel="matrix",
            agent_name=agent_name,
            runtime_paths=self.runtime_paths,
            requester_id=requester_id,
            room_id=ROOM,
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
        )
        return resolve_agent_execution(agent_name, self.config, self.runtime_paths, identity).worker_target

    def provider(self, provider_id: str = PROVIDER) -> OAuthProvider:
        return config_plugins(self.config, self.runtime_paths).oauth_providers[provider_id]

    def tools_scope(self, provider_id: str, agent_name: str, requester_id: str) -> ResolvedWorkerTarget | None:
        """Return the scope whose connection to the provider the agent's tools use for ``requester_id``."""
        return resolve_oauth_credential_context(
            self.provider(provider_id),
            self.runtime_paths,
            get_runtime_credentials_manager(self.runtime_paths),
            self.target(agent_name, requester_id),
            config=self.config,
        ).worker_target

    def serve(self, agents: dict[str, Any]) -> None:
        """Serve and run a config with ``agents``, as an operator's config save and the reload after it do."""
        state = require_api_state(self.request.app)
        config = _config(self.runtime_paths, agents)
        state.snapshot = ApiSnapshot(state.snapshot.generation + 1, self.runtime_paths, runtime_config=config)
        self.runtime.config = config

    def connect(
        self,
        target: ResolvedWorkerTarget | None,
        credentials: dict[str, Any],
        provider_id: str = PROVIDER,
    ) -> None:
        """Store a connection to the provider in ``target``'s scope, as signing in through the agent's link does."""
        publish_oauth_credentials(
            self.provider(provider_id),
            credentials,
            credentials_manager=get_runtime_credentials_manager(self.runtime_paths),
            worker_target=target,
        )

    def stored(self, target: ResolvedWorkerTarget) -> dict[str, Any]:
        """Return the connection to the provider stored in ``target``'s scope."""
        context = OAuthCredentialContext(
            self.provider(),
            self.runtime_paths,
            get_runtime_credentials_manager(self.runtime_paths),
            target,
        )
        credentials = load_oauth_credentials_snapshot_sync(context).credentials
        assert credentials is not None
        return credentials

    async def token(self, user_id: str, provider_id: str = PROVIDER, agent_name: str = "assistant") -> str:
        return await client_user_oauth_access_token(self.request, ClientUser(user_id), provider_id, agent_name)


def _config(paths: RuntimePaths, agents: dict[str, Any]) -> Config:
    return bind_runtime_paths(
        Config(
            agents=agents,
            plugins=["./plugins/drive"],
            authorization={"aliases": {ALICE: [ALICE_BRIDGED]}},
            bot_accounts=[BRIDGE_BOT],
        ),
        paths,
    )


def _deployment(tmp_path: Path, **env: str) -> _Deployment:
    plugin = tmp_path / "plugins" / "drive"
    plugin.mkdir(parents=True)
    (plugin / "mindroom.plugin.json").write_text(json.dumps({"name": "drive", "oauth_module": "oauth.py"}))
    (plugin / "oauth.py").write_text(OAUTH_MODULE)
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": "", **env},
    )
    config = _config(paths, AGENTS)
    publish_test_plugins(config, paths)
    manager = get_runtime_credentials_manager(paths)
    for client_service in ("acme_drive_oauth_client", "acme_code_oauth_client"):
        manager.save_credentials(client_service, {"client_id": "acme-client", "client_secret": "acme-secret"})
    manager.save_credentials("google_oauth_client", {"client_id": "google-client", "client_secret": "google-secret"})
    update_matrix_state(paths, lambda state: state.add_room("lounge", LOUNGE, "#lounge:example.com", "Lounge"))
    app = FastAPI()
    state = ensure_app_state(app)
    state.api_state = ApiState(threading.Lock(), ApiSnapshot(1, paths, runtime_config=config))
    runtime = _Runtime(config, paths)
    state.embedded = cast("EmbeddedRuntime", runtime)
    request = Request({"type": "http", "app": app, PLUGIN_NAME_SCOPE_KEY: "drive"})
    return _Deployment(config, paths, request, runtime)


def _record_store_opens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the provider of each OAuth credential store opened from now on; each still opens as it would."""
    opened: list[str] = []
    open_database = credential_store._open_oauth_database

    def recording(context: OAuthCredentialContext) -> Any:  # noqa: ANN401 - the store's own context manager
        opened.append(context.provider.id)
        return open_database(context)

    monkeypatch.setattr(credential_store, "_open_oauth_database", recording)
    return opened


def _credentials(
    token: str,
    *,
    expires_in: float = 3600,
    client_id: str = "acme-client",
    scopes: tuple[str, ...] = ("read",),
    token_uri: str = TOKEN_URL,
) -> dict[str, Any]:
    return {
        "token": token,
        "refresh_token": f"{token}-refresh",
        "expires_at": time.time() + expires_in,
        "client_id": client_id,
        "scopes": list(scopes),
        "token_uri": token_uri,
    }


@pytest.mark.asyncio
async def test_client_user_oauth_access_token_returns_callers_own_token(tmp_path: Path) -> None:
    """The caller gets the token of the connection the agent's tools use for them.

    The agent's access names Alice, so no room is read, though another agent's room grants are not read yet.
    """
    deployment = _deployment(tmp_path)
    deployment.connect(deployment.target("assistant", ALICE), _credentials("alice-token"))

    assert await deployment.token(ALICE) == "alice-token"
    assert deployment.runtime.room_reads == 0


@pytest.mark.parametrize("agent_name", ["helper", "notes"], ids=["worker_scope_user", "private"])
@pytest.mark.asyncio
async def test_client_user_oauth_access_token_answers_each_agent_that_keeps_connections_per_person(
    tmp_path: Path,
    agent_name: str,
) -> None:
    """An agent with `worker_scope: user`, or a private one, keeps each person's connection apart, so each gets theirs."""
    deployment = _deployment(tmp_path)
    deployment.connect(deployment.target(agent_name, ALICE), _credentials("alice-token"))
    deployment.connect(deployment.target(agent_name, BOB), _credentials("bob-token"))

    assert await deployment.token(ALICE, agent_name=agent_name) == "alice-token"
    assert await deployment.token(BOB, agent_name=agent_name) == "bob-token"


@pytest.mark.asyncio
async def test_client_user_oauth_access_token_never_reads_another_users_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only Bob connected; Alice is asked to connect her own account for this agent, with a link that is hers."""
    deployment = _deployment(tmp_path)
    deployment.connect(deployment.target("assistant", BOB), _credentials("bob-token"))
    opened = _record_store_opens(monkeypatch)

    with pytest.raises(OAuthConnectionRequired) as raised:
        await deployment.token(ALICE)

    # Her store was read, and read again for the link: the refusals below would have shown such opens.
    assert opened
    assert set(opened) == {PROVIDER}

    connect_url = raised.value.connect_url
    assert connect_url
    assert "bob-token" not in str(raised.value)
    query = parse_qs(urlparse(connect_url).query)
    assert (query["agent_name"], query["execution_scope"]) == (["assistant"], ["user_agent"])
    link = lookup_oauth_connect_token(deployment.provider(), deployment.runtime_paths, query["connect_token"][0])
    assert link.requester_id == ALICE
    assert link.binding.worker_key == deployment.target("assistant", ALICE).worker_key


@pytest.mark.asyncio
async def test_client_user_oauth_access_token_unreadable_connection_asks_for_a_reset(tmp_path: Path) -> None:
    """A stored connection that cannot be read gets no sign-in link: it must first be reset in the dashboard."""
    deployment = _deployment(tmp_path)
    deployment.connect(deployment.target("assistant", ALICE), _credentials("alice-token"))
    (database,) = tmp_path.rglob("acme_drive_oauth_credentials.sqlite3")
    corrupt_oauth_credential_payload(database, b"not a credential payload")

    with pytest.raises(OAuthConnectionRequired) as raised:
        await deployment.token(ALICE)

    assert (raised.value.reset_required, raised.value.connect_url) == (True, None)


@pytest.mark.asyncio
async def test_client_user_oauth_access_token_refuses_shared_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared agent's connection serves everyone who uses it, so it is refused before anything is read.

    Its token is about to expire, so reading it would refresh it at the token endpoint.
    """
    deployment = _deployment(tmp_path)
    deployment.connect(deployment.target("team", ALICE), _credentials("team-token", expires_in=10))
    refreshes = serve_token_endpoint(monkeypatch, [rotated_token_response()])
    opened = _record_store_opens(monkeypatch)

    with pytest.raises(PermissionError):
        await deployment.token(ALICE, agent_name="team")

    assert (opened, refreshes, deployment.runtime.room_reads) == ([], [], 0)


@pytest.mark.asyncio
async def test_client_user_oauth_access_token_refuses_unscoped_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent without a worker scope uses the global connection, which serves everyone, so it is refused too."""
    deployment = _deployment(tmp_path)
    deployment.connect(None, _credentials("global-token", expires_in=10))
    refreshes = serve_token_endpoint(monkeypatch, [rotated_token_response()])
    opened = _record_store_opens(monkeypatch)

    with pytest.raises(PermissionError):
        await deployment.token(ALICE, agent_name="lobby")

    assert (opened, refreshes, deployment.runtime.room_reads) == ([], [], 0)


@pytest.mark.parametrize("agent_name", ["assistant", "notes"], ids=["worker_scope_user_agent", "private"])
@pytest.mark.asyncio
async def test_client_user_oauth_access_token_refuses_caller_whose_access_was_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_name: str,
) -> None:
    """Once the operator takes Alice off an agent, her connection there is not read, though it is still stored."""
    deployment = _deployment(tmp_path)
    deployment.connect(deployment.target(agent_name, ALICE), _credentials("alice-token", expires_in=10))
    deployment.serve({**AGENTS, agent_name: {**AGENTS[agent_name], "access": {"users": [BOB]}}})
    refreshes = serve_token_endpoint(monkeypatch, [rotated_token_response()])
    opened = _record_store_opens(monkeypatch)

    with pytest.raises(PermissionError):
        await deployment.token(ALICE, agent_name=agent_name)

    assert (opened, refreshes, deployment.runtime.room_reads) == ([], [], 0)


@pytest.mark.asyncio
async def test_client_user_oauth_access_token_refuses_agent_the_caller_may_not_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alice may not use Bob's agent, so she gets a refusal, not a link to connect an account she could never use.

    The agent's access names people, not rooms, so no room is read for her, however often she asks and though a room
    another agent's access rests on cannot be read.
    """
    deployment = _deployment(tmp_path)
    deployment.runtime.client.joined_rooms.return_value = nio.JoinedRoomsError("Unavailable")
    opened = _record_store_opens(monkeypatch)

    for _ in range(3):
        with pytest.raises(PermissionError):
            await deployment.token(ALICE, agent_name="bobs")

    assert (opened, deployment.runtime.room_reads) == ([], 0)
    with pytest.raises(OAuthConnectionRequired):
        await deployment.token(BOB, agent_name="bobs")


@pytest.mark.asyncio
async def test_client_user_oauth_access_token_reads_room_grants_that_need_it_first(tmp_path: Path) -> None:
    """Alice may use the lounge's agent as a member of the lounge, which the runtime has not read yet; it is read first.

    Once read, the grants stay current, so her next request reads no room again.
    """
    deployment = _deployment(tmp_path)
    deployment.connect(deployment.target("lounge", ALICE), _credentials("alice-token"))
    assert deployment.runtime.agent_reply_memberships.needs_refresh(deployment.config)

    assert await deployment.token(ALICE, agent_name="lounge") == "alice-token"
    assert deployment.runtime.room_reads == 1
    assert await deployment.token(ALICE, agent_name="lounge") == "alice-token"
    assert deployment.runtime.room_reads == 1


@pytest.mark.asyncio
async def test_client_user_oauth_access_token_refuses_non_member_after_reading_room_grants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bob may use the lounge's agent only as a member of the lounge; once the lounge is read, he is refused."""
    deployment = _deployment(tmp_path)
    deployment.connect(deployment.target("lounge", BOB), _credentials("bob-token"))
    opened = _record_store_opens(monkeypatch)

    with pytest.raises(PermissionError):
        await deployment.token(BOB, agent_name="lounge")

    assert (opened, deployment.runtime.room_reads) == ([], 1)


@pytest.mark.asyncio
async def test_client_user_oauth_access_token_answers_503_while_access_cannot_be_decided(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Access that rests on a room no one can read now, or without the runtime's router, is unknown, not refused.

    An answer that needs no room is still given without the runtime: Alice's access to `assistant` names her.
    """
    deployment = _deployment(tmp_path)
    deployment.connect(deployment.target("lounge", ALICE), _credentials("alice-lounge-token"))
    deployment.connect(deployment.target("assistant", ALICE), _credentials("alice-token"))
    deployment.runtime.client.joined_rooms.return_value = nio.JoinedRoomsError("Unavailable")
    opened = _record_store_opens(monkeypatch)

    with pytest.raises(HTTPException) as unreadable:
        await deployment.token(ALICE, agent_name="lounge")
    assert (unreadable.value.status_code, opened, deployment.runtime.room_reads) == (503, [], 1)

    ensure_app_state(deployment.request.app).embedded = None
    with pytest.raises(HTTPException) as without_bots:
        await deployment.token(ALICE, agent_name="lounge")
    assert (without_bots.value.status_code, opened) == (503, [])
    assert await deployment.token(ALICE) == "alice-token"


@pytest.mark.asyncio
async def test_client_user_oauth_access_token_refuses_bot_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bot account the agent converses with may not connect its accounts, as its connect link would show, so no token.

    No room grant could change that, so no room is read for it, even for an agent whose access rests on a room.
    """
    deployment = _deployment(tmp_path)
    deployment.serve({**AGENTS, "assistant": {**AGENTS["assistant"], "access": {"users": [ALICE, BOB, BRIDGE_BOT]}}})
    opened = _record_store_opens(monkeypatch)

    for agent_name in ("assistant", "lounge"):
        with pytest.raises(PermissionError):
            await deployment.token(BRIDGE_BOT, agent_name=agent_name)

    assert (opened, deployment.runtime.room_reads) == ([], 0)


@pytest.mark.asyncio
async def test_client_user_oauth_access_token_bridge_alias_reaches_its_persons_own_connection(tmp_path: Path) -> None:
    """Alice's bridged account is Alice, so it gets her connection, as the agent's tools use it for her, and never Bob's."""
    deployment = _deployment(tmp_path)
    deployment.connect(deployment.target("assistant", ALICE), _credentials("alice-token"))
    deployment.connect(deployment.target("assistant", BOB), _credentials("bob-token"))

    assert await deployment.token(ALICE_BRIDGED) == "alice-token"
    # Read under its own ID, the bridged account would have had a store of its own.
    assert deployment.target("assistant", ALICE_BRIDGED).worker_key != deployment.target("assistant", ALICE).worker_key


@pytest.mark.asyncio
async def test_client_user_oauth_access_token_requester_scoped_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider whose connection is the person's own across agents is answered for an agent that keeps it apart.

    The shared agent's tools use that same connection, but the route still refuses it there before opening any store.
    """
    deployment = _deployment(tmp_path)
    alice = deployment.tools_scope("acme_code", "team", ALICE)
    in_assistant = deployment.tools_scope("acme_code", "assistant", ALICE)
    assert alice is not None
    assert in_assistant is not None
    # Both agents' tools use Alice's one store.
    assert (alice.worker_scope, alice.worker_key) == ("user", in_assistant.worker_key)
    deployment.connect(alice, _credentials("alice-code-token"), provider_id="acme_code")
    deployment.connect(deployment.tools_scope("acme_code", "team", BOB), _credentials("bob-code-token"), "acme_code")

    assert await deployment.token(ALICE, provider_id="acme_code") == "alice-code-token"
    opened = _record_store_opens(monkeypatch)
    with pytest.raises(PermissionError):
        await deployment.token(ALICE, provider_id="acme_code", agent_name="team")
    assert opened == []


@pytest.mark.asyncio
async def test_client_user_oauth_access_token_refreshes_expiring_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token about to expire is refreshed first, and the agent's tools then use the new one too."""
    deployment = _deployment(tmp_path)
    alice = deployment.target("assistant", ALICE)
    deployment.connect(alice, _credentials("expiring-token", expires_in=10))
    refreshes = serve_token_endpoint(monkeypatch, [rotated_token_response()])

    assert await deployment.token(ALICE) == "rotated-access-token"

    assert refreshes == ["expiring-token-refresh"]
    stored = deployment.stored(alice)
    assert (stored["token"], stored["refresh_token"]) == ("rotated-access-token", "rotated-refresh-token")


@pytest.mark.asyncio
async def test_client_user_oauth_access_token_refresh_failure_is_retryable_and_names_no_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh the provider fails for now is an error a retry may fix, and no log record or message holds a token."""
    deployment = _deployment(tmp_path)
    alice = deployment.target("assistant", ALICE)
    deployment.connect(alice, _credentials("expiring-token", expires_in=10))
    serve_token_endpoint(monkeypatch, [httpx.Response(503, json={"error": "temporarily_unavailable"})])

    with capture_logs() as logs, pytest.raises(OAuthProviderError) as raised:
        await deployment.token(ALICE)

    assert not isinstance(raised.value, OAuthConnectionRequired)
    assert logs
    for text in (str(raised.value), *map(repr, logs)):
        assert "expiring-token" not in text
    # The connection stays, so the next call refreshes it again.
    stored = deployment.stored(alice)
    assert (stored["token"], stored["refresh_token"]) == ("expiring-token", "expiring-token-refresh")


@pytest.mark.asyncio
async def test_client_user_oauth_access_token_unknown_provider_or_agent(tmp_path: Path) -> None:
    """A provider or agent the config does not have is a lookup error, not a connect prompt."""
    deployment = _deployment(tmp_path)

    with pytest.raises(LookupError, match="no_such_provider"):
        await deployment.token(ALICE, provider_id="no_such_provider")
    with pytest.raises(LookupError, match="no_such_agent"):
        await deployment.token(ALICE, agent_name="no_such_agent")


@pytest.mark.asyncio
async def test_client_user_oauth_access_token_ignores_service_account(tmp_path: Path) -> None:
    """With a Google service account configured, the caller still gets only their own connection or a connect prompt."""
    service_account = tmp_path / "service-account.json"
    service_account.write_text(json.dumps({"type": "service_account", "client_email": "robot@example.com"}))
    deployment = _deployment(tmp_path, GOOGLE_SERVICE_ACCOUNT_FILE=str(service_account))
    google_drive = deployment.provider("google_drive")
    deployment.connect(
        deployment.target("assistant", ALICE),
        _credentials(
            "alice-google-token",
            client_id="google-client",
            scopes=google_drive.scopes,
            token_uri=GOOGLE_TOKEN_URL,
        ),
        provider_id="google_drive",
    )

    assert await deployment.token(ALICE, provider_id="google_drive") == "alice-google-token"
    with pytest.raises(OAuthConnectionRequired):
        await deployment.token(BOB, provider_id="google_drive")
