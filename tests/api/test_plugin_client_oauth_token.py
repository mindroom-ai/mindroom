"""A plugin route reads the access token of its caller's own OAuth connection, as the agent's tools use it for them.

It answers only a connection kept for that one person, never a shared or unscoped agent's, nor a service account's.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI, Request
from structlog.testing import capture_logs

from mindroom.api.app_state import ensure_app_state
from mindroom.api.config_lifecycle import ApiSnapshot, ApiState
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.oauth.credential_lifecycle import OAuthCredentialContext, load_oauth_credentials_snapshot_sync
from mindroom.oauth.providers import OAuthProviderError
from mindroom.oauth.service import lookup_oauth_connect_token
from mindroom.plugin_api import OAuthConnectionRequired
from mindroom.plugin_http import ClientUser, client_user_oauth_access_token
from mindroom.runtime_resolution import resolve_agent_execution
from mindroom.tool_system.plugins import PLUGIN_NAME_SCOPE_KEY, config_plugins
from mindroom.tool_system.worker_routing import build_tool_execution_identity
from tests.conftest import bind_runtime_paths, publish_test_plugins
from tests.oauth_test_utils import publish_oauth_credentials, rotated_token_response, serve_token_endpoint

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.oauth import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

ALICE = "@alice:example.com"
BOB = "@bob:example.com"
ROOM = "!room:example.com"
PROVIDER = "acme_drive"
TOKEN_URL = "https://accounts.example.com/oauth/token"  # noqa: S105
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105
# One agent keeps each person's connections apart; the other two share theirs among everyone who uses them.
AGENTS = {
    "assistant": {"display_name": "Assistant", "worker_scope": "user_agent"},
    "team": {"display_name": "Team", "worker_scope": "shared"},
    "lobby": {"display_name": "Lobby"},
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
    ]
"""


@dataclass(frozen=True)
class _Deployment:
    """A MindRoom whose API serves the plugin ``drive``, which brings the provider ``acme_drive``, and ``AGENTS``."""

    config: Config
    runtime_paths: RuntimePaths
    request: Request
    """A request one of the plugin's routes serves."""

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
    config = bind_runtime_paths(Config(agents=AGENTS, plugins=["./plugins/drive"]), paths)
    publish_test_plugins(config, paths)
    manager = get_runtime_credentials_manager(paths)
    manager.save_credentials("acme_drive_oauth_client", {"client_id": "acme-client", "client_secret": "acme-secret"})
    manager.save_credentials("google_oauth_client", {"client_id": "google-client", "client_secret": "google-secret"})
    app = FastAPI()
    ensure_app_state(app).api_state = ApiState(threading.Lock(), ApiSnapshot(1, paths, runtime_config=config))
    return _Deployment(config, paths, Request({"type": "http", "app": app, PLUGIN_NAME_SCOPE_KEY: "drive"}))


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
    """The caller gets the token of the connection the agent's tools use for them."""
    deployment = _deployment(tmp_path)
    deployment.connect(deployment.target("assistant", ALICE), _credentials("alice-token"))

    assert await deployment.token(ALICE) == "alice-token"


@pytest.mark.asyncio
async def test_client_user_oauth_access_token_never_reads_another_users_connection(tmp_path: Path) -> None:
    """Only Bob connected; Alice is asked to connect her own account for this agent, with a link that is hers."""
    deployment = _deployment(tmp_path)
    deployment.connect(deployment.target("assistant", BOB), _credentials("bob-token"))

    with pytest.raises(OAuthConnectionRequired) as raised:
        await deployment.token(ALICE)

    connect_url = raised.value.connect_url
    assert connect_url
    assert "bob-token" not in str(raised.value)
    query = parse_qs(urlparse(connect_url).query)
    assert (query["agent_name"], query["execution_scope"]) == (["assistant"], ["user_agent"])
    link = lookup_oauth_connect_token(deployment.provider(), deployment.runtime_paths, query["connect_token"][0])
    assert link.requester_id == ALICE
    assert link.binding.worker_key == deployment.target("assistant", ALICE).worker_key


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

    with pytest.raises(PermissionError):
        await deployment.token(ALICE, agent_name="team")

    assert refreshes == []


@pytest.mark.asyncio
async def test_client_user_oauth_access_token_refuses_unscoped_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent without a worker scope uses the global connection, which serves everyone, so it is refused too."""
    deployment = _deployment(tmp_path)
    deployment.connect(None, _credentials("global-token", expires_in=10))
    refreshes = serve_token_endpoint(monkeypatch, [rotated_token_response()])

    with pytest.raises(PermissionError):
        await deployment.token(ALICE, agent_name="lobby")

    assert refreshes == []


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
