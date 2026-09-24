"""Shared fixtures for Atlassian Cloud toolkit tests: grants, sites, and a mocked gateway."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import httpx

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials import CredentialsManager, get_runtime_credentials_manager
from mindroom.custom_tools import atlassian_client
from mindroom.message_target import MessageTarget
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import bind_runtime_paths, make_conversation_reader_mock, make_relation_lookup
from tests.oauth_test_utils import publish_oauth_credentials

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

CLOUD_ID = "00000000-0000-4000-8000-000000000001"
OTHER_CLOUD_ID = "00000000-0000-4000-8000-000000000002"
SITE_URL = "https://example.atlassian.net"
OTHER_SITE_URL = "https://acme.atlassian.net"
ALICE = "@alice:example.org"
BOB = "@bob:example.org"
GATEWAY = "https://api.atlassian.com"
JIRA_SCOPES = ["read:jira-work", "write:jira-work"]
CONFLUENCE_SCOPES = [
    "search:confluence",
    "read:confluence-content.all",
    "readonly:content.attachment:confluence",
    "write:page:confluence",
    "write:comment:confluence",
    "read:space:confluence",
]

type Handler = Callable[[httpx.Request], httpx.Response]


def runtime_paths(tmp_path: Path, extra_env: dict[str, str] | None = None) -> RuntimePaths:
    """Return runtime paths with a public callback origin."""
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MINDROOM_PUBLIC_URL": "https://chat.example.com", **(extra_env or {})},
    )


def save_client_config(paths: RuntimePaths, service: str = "atlassian_oauth_client") -> CredentialsManager:
    """Store one Atlassian app's client credentials."""
    manager = get_runtime_credentials_manager(paths)
    manager.save_credentials(service, {"client_id": "atlassian-client", "client_secret": "atlassian-secret"})
    return manager


def execution_identity(requester_id: str = ALICE) -> ToolExecutionIdentity:
    """Return one requester's tool execution identity for the test agent."""
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="assistant",
        requester_id=requester_id,
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=None,
    )


def worker_target(requester_id: str = ALICE) -> ResolvedWorkerTarget:
    """Return the requester-owned target that requester-scoped OAuth credentials resolve to."""
    return resolve_worker_target("user", "assistant", execution_identity=execution_identity(requester_id))


def grant(token: str, provider: OAuthProvider, **overrides: object) -> dict[str, object]:
    """Return stored OAuth credentials holding every scope the provider requests."""
    return {
        "token": token,
        "refresh_token": f"{token}-refresh",
        "client_id": "atlassian-client",
        "scopes": list(provider.scopes),
        "expires_at": 4_102_444_800.0,
        "_source": "oauth",
        "_oauth_provider": provider.id,
        **overrides,
    }


def publish_grant(
    provider: OAuthProvider,
    manager: CredentialsManager,
    token: str,
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


def site(
    cloud_id: str = CLOUD_ID,
    url: str = SITE_URL,
    scopes: list[str] | None = None,
    name: str = "example",
) -> dict[str, object]:
    """Return one accessible-resources entry."""
    return {"id": cloud_id, "url": url, "name": name, "scopes": scopes or [*JIRA_SCOPES, *CONFLUENCE_SCOPES]}


def bearer(request: httpx.Request) -> str | None:
    """Return the bearer token a request carried, if any."""
    authorization = request.headers.get("authorization")
    return authorization.removeprefix("Bearer ") if authorization else None


@dataclass
class FakeGateway:
    """Mocked Atlassian gateway: accessible resources per token plus routed product calls."""

    sites_by_token: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    routes: dict[tuple[str, str], Handler] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def route(self, method: str, url: str, handler: Handler | dict[str, Any] | list[Any] | None = None) -> None:
        """Answer one method and URL (without query) with a handler or a JSON body."""
        if handler is None or isinstance(handler, dict | list):
            body = handler

            def respond(_request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json=body) if body is not None else httpx.Response(204)

            self.routes[(method, url)] = respond
        else:
            self.routes[(method, url)] = handler

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Record and answer one request."""
        self.requests.append(request)
        url = str(request.url.copy_with(query=None))
        if url == f"{GATEWAY}/oauth/token/accessible-resources":
            token = bearer(request)
            if token not in self.sites_by_token:
                return httpx.Response(401, json={"message": "Unauthorized"})
            return httpx.Response(200, json=self.sites_by_token[token])
        handler = self.routes.get((request.method, url))
        if handler is None:
            return httpx.Response(404, json={"message": f"no route for {request.method} {url}"})
        return handler(request)

    def product_requests(self) -> list[httpx.Request]:
        """Return requests other than site discovery."""
        return [request for request in self.requests if not request.url.path.startswith("/oauth/")]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeGateway:
        """Route every Atlassian client request through this fake."""
        real_client = httpx.AsyncClient

        def client_factory() -> httpx.AsyncClient:
            return real_client(transport=httpx.MockTransport(self.handle), follow_redirects=False)

        monkeypatch.setattr(atlassian_client, "_new_http_client", client_factory)
        return self


def gateway_url(product: str, path: str, cloud_id: str = CLOUD_ID) -> str:
    """Return the gateway URL for one product path on a site."""
    return f"{GATEWAY}/ex/{product}/{cloud_id}{path}"


def json_body(request: httpx.Request) -> Any:  # noqa: ANN401
    """Decode a request's JSON body."""
    return json.loads(request.content)


def tool_context(paths: RuntimePaths, storage_path: Path, *, requester_id: str = ALICE) -> ToolRuntimeContext:
    """Return a conversation context with MindRoom attachment storage."""
    return make_test_tool_runtime_context(
        agent_name="assistant",
        target=MessageTarget.resolve(room_id="!room:example.org", thread_id="$thread", reply_to_event_id=None),
        requester_id=requester_id,
        client=MagicMock(),
        config=bind_runtime_paths(Config(), paths),
        runtime_paths=paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        storage_path=storage_path,
    )
