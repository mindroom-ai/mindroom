"""Shared fixtures for Microsoft 365 toolkit tests: grants and a mocked Microsoft Graph."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

import httpx

from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials.manager import CredentialsManager, get_runtime_credentials_manager
from mindroom.custom_tools import microsoft_graph
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from tests.oauth_test_utils import publish_oauth_credentials

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

ALICE = "@alice:example.org"
TOKEN = "graph-access-token"

type Handler = Callable[[httpx.Request], httpx.Response]


def runtime_paths(tmp_path: Path, extra_env: dict[str, str] | None = None) -> RuntimePaths:
    """Return runtime paths with a public callback origin."""
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MINDROOM_PUBLIC_URL": "https://chat.example.com", **(extra_env or {})},
    )


def save_client_config(paths: RuntimePaths) -> CredentialsManager:
    """Store the shared Microsoft app's client credentials."""
    manager = get_runtime_credentials_manager(paths)
    manager.save_credentials(
        "microsoft_oauth_client",
        {"client_id": "microsoft-client", "client_secret": "microsoft-secret"},
    )
    return manager


def worker_target(requester_id: str = ALICE) -> ResolvedWorkerTarget:
    """Return the requester-owned target that requester-scoped OAuth credentials resolve to."""
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="assistant",
        requester_id=requester_id,
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=None,
    )
    return resolve_worker_target("user", "assistant", execution_identity=identity)


def grant(token: str, provider: OAuthProvider, **overrides: object) -> dict[str, object]:
    """Return stored OAuth credentials holding every scope the provider requests, as Entra reports them."""
    return {
        "token": token,
        "refresh_token": f"{token}-refresh",
        "token_uri": provider.token_url,
        "client_id": "microsoft-client",
        "scopes": [scope for scope in provider.scopes if scope != "offline_access"],
        "expires_at": 4_102_444_800.0,
        "_source": "oauth",
        "_oauth_provider": provider.id,
        **overrides,
    }


def publish_grant(
    provider: OAuthProvider,
    manager: CredentialsManager,
    token: str = TOKEN,
    *,
    requester_id: str = ALICE,
    **overrides: object,
) -> None:
    """Publish one requester's grant through the real OAuth credential store."""
    publish_oauth_credentials(
        provider,
        grant(token, provider, **overrides),
        credentials_manager=manager,
        worker_target=worker_target(requester_id),
    )


def bearer(request: httpx.Request) -> str | None:
    """Return the bearer token a request carried, if any."""
    authorization = request.headers.get("authorization")
    return authorization.removeprefix("Bearer ") if authorization else None


@dataclass
class FakeGraph:
    """Answer every Graph client request through one handler and record the requests."""

    handler: Handler
    requests: list[httpx.Request] = field(default_factory=list)

    def _record(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        return self.handler(request)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeGraph:
        """Route the Graph client's HTTP through this fake."""
        real = microsoft_graph._new_http_client
        monkeypatch.setattr(
            microsoft_graph,
            "_new_http_client",
            partial(real, transport=httpx.MockTransport(self._record)),
        )
        return self

    def calls(self) -> list[str]:
        """Return each request as `METHOD host/path`."""
        return [f"{request.method} {request.url.host}{request.url.path}" for request in self.requests]

