"""Real HTTP OAuth and personal MCP authorization boundaries."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from html.parser import HTMLParser
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from agno.tools import Toolkit
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindroom import agents, constants
from mindroom.api import config_lifecycle, main
from mindroom.api.mcp_gateway import GatewayRuntime, gateway_lifespan, install_gateway_routes
from mindroom.config.main import Config
from mindroom.mcp_gateway import admission
from mindroom.tool_system.worker_routing import get_tool_execution_identity
from tests.api.test_api import _trusted_upstream_jwks, _trusted_upstream_jwt, _trusted_upstream_jwt_key

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path

    import httpx
    from starlette.requests import Request
    from starlette.responses import Response

ORIGIN = "https://assistant.example.org"
RESOURCE = ORIGIN + "/mcp"
CALLBACK = "https://client.example.org/callback?flag=&tenant=one"
VERIFIER = "v" * 64
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}


class _Inputs(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.values: dict[str, str] = {}
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "input" and values.get("name") and values.get("value"):
            self.values[str(values["name"])] = str(values["value"])


@pytest.fixture
def signed_headers(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], dict[str, str]]:
    """Exercise production JWT verification with locally signed identities."""
    key = _trusted_upstream_jwt_key()
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _client: _trusted_upstream_jwks(key))

    def headers(user: str = "alice") -> dict[str, str]:
        return {
            "X-Trusted-User": user,
            "X-Trusted-Email": f"{user}@example.org",
            "X-Trusted-Jwt": _trusted_upstream_jwt(
                key,
                user_id=user,
                email=f"{user}@example.org",
                matrix_user_id=f"@{user}:example.org",
                issuer="https://issuer.example.org",
            ),
        }

    return headers


@pytest.fixture
def gateway_app(tmp_path: Path) -> FastAPI:
    """Build the production gateway routes without starting Matrix or an LLM."""
    env = {
        "MINDROOM_PUBLIC_URL": ORIGIN,
        "MINDROOM_MCP_GATEWAY_ENABLED": "true",
        "MINDROOM_CONNECTIONS_AGENT": "personal",
        "MATRIX_HOMESERVER": "https://example.org",
        "MINDROOM_API_KEY": "owner-key",
        "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
        "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER": "X-Trusted-User",
        "MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER": "X-Trusted-Email",
        "MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT": "true",
        "MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER": "X-Trusted-Jwt",
        "MINDROOM_TRUSTED_UPSTREAM_JWKS_URL": "https://issuer.example.org/jwks",
        "MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE": "mindroom-dashboard",
        "MINDROOM_TRUSTED_UPSTREAM_JWT_ISSUER": "https://issuer.example.org",
        "MINDROOM_TRUSTED_UPSTREAM_JWT_USER_ID_CLAIM": "sub",
        "MINDROOM_TRUSTED_UPSTREAM_JWT_MATRIX_USER_ID_CLAIM": "matrix_user_id",
    }
    paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env=env,
    )
    config = Config.model_validate(
        {
            "agents": {
                "personal": {
                    "display_name": "Personal assistant",
                    "role": "Help",
                    "tools": ["calculator"],
                    "private": {"per": "user_agent"},
                    "access": {"users": ["@alice:example.org", "@bob:example.org"]},
                },
            },
        },
    )
    app = FastAPI(lifespan=gateway_lifespan)
    main.initialize_api_app(app, paths)
    main._add_dashboard_cors_middleware(app, paths)
    snapshot = config_lifecycle.require_api_state(app).snapshot
    snapshot.runtime_config = config
    snapshot.config_data = config.model_dump()
    install_gateway_routes(app)
    return app


@pytest.fixture
def gateway_client(gateway_app: FastAPI) -> Iterator[TestClient]:
    """Run the gateway's real SDK and manager lifecycle."""
    with TestClient(gateway_app, base_url=ORIGIN, follow_redirects=False) as client:
        yield client


def _set_peer(client: TestClient, host: str | None, port: int = 50000) -> None:
    client._transport.client = None if host is None else (host, port)


def _set_onboarding_limits(gateway_app: FastAPI, *, aggregate: int, source: int) -> None:
    snapshot = config_lifecycle.require_api_state(gateway_app).snapshot
    snapshot.runtime_paths = replace(
        snapshot.runtime_paths,
        process_env={
            **snapshot.runtime_paths.process_env,
            "MINDROOM_MCP_GATEWAY_ONBOARDING_RATE_LIMIT": str(aggregate),
            "MINDROOM_MCP_GATEWAY_ONBOARDING_SOURCE_RATE_LIMIT": str(source),
        },
    )


def _authorize(client: TestClient) -> tuple[str, str]:
    registration = client.post(
        "/mcp/oauth/register",
        json={
            "client_name": "Local client <script>alert(1)</script>",
            "redirect_uris": [CALLBACK],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "mcp:tools",
        },
    )
    assert registration.status_code == 201, registration.text
    client_id = registration.json()["client_id"]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
    response = client.get(
        "/mcp/oauth/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": RESOURCE,
            "scope": "mcp:tools",
            "state": "client-state",
        },
    )
    assert response.status_code in {302, 303, 307}, response.text
    return client_id, response.headers["location"]


def _consent(client: TestClient, url: str, headers: dict[str, str]) -> dict[str, str]:
    response = client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    assert "<script>" not in response.text
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["referrer-policy"] == "strict-origin"
    return _Inputs(response.text).values


def _code(client: TestClient, headers: dict[str, str]) -> tuple[str, str]:
    client_id, url = _authorize(client)
    fields = _consent(client, url, headers)
    response = client.post(
        "/connections/mcp/authorize",
        data={**fields, "decision": "allow"},
        headers={**headers, "Origin": ORIGIN},
    )
    assert response.status_code == 303, response.text
    callback = response.headers["location"]
    assert callback.startswith(CALLBACK + "&")
    assert parse_qs(urlsplit(callback).query)["state"] == ["client-state"]
    return client_id, parse_qs(urlsplit(callback).query)["code"][0]


def _exchange(client: TestClient, client_id: str, code: str, **overrides: str) -> httpx.Response:
    return client.post(
        "/mcp/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": CALLBACK,
            "code_verifier": VERIFIER,
            "resource": RESOURCE,
            **overrides,
        },
    )


def _list(client: TestClient, token: str | None = None, **headers: str) -> httpx.Response:
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            **MCP_HEADERS,
            **({"Authorization": f"Bearer {token}"} if token else {}),
            **headers,
        },
    )


def _native_dispatch_builder(
    phase: str,
    async_body: bool,
    paused: threading.Event,
    release: threading.Event,
    events: list[str],
) -> Callable[..., Toolkit]:
    def pause(at: str) -> None:
        if phase != at:
            return
        paused.set()
        assert release.wait(10), "Dispatch was never released"

    def account() -> str:
        identity = get_tool_execution_identity()
        assert identity is not None
        assert identity.requester_id == "@alice:example.org"
        assert identity.agent_name == "personal"
        events.append("body")
        return "account-result"

    async def async_account() -> str:
        return account()

    async def before(name: str, func: Callable[..., Awaitable[str]], args: dict[str, object]) -> str:
        assert name in {"account", "async_account"}
        await asyncio.to_thread(pause, "hook")
        return await func(**args)

    class AccountToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[async_account if async_body else account])
            self._requires_connect = True
            function = next(iter({**self.functions, **self.async_functions}.values()))
            function.tool_hooks = [before]

        def connect(self) -> None:
            pause("connect")

        def close(self) -> None:
            events.append("close")

    def build(name: str, **_kwargs: object) -> Toolkit:
        assert name == "calculator", "Unselected toolkit was built"
        pause("build")
        return AccountToolkit()

    return build


@pytest.mark.parametrize("phase", ["build", "connect", "hook"])
@pytest.mark.parametrize("change", ["remove", "scope", "access", "unchanged"])
@pytest.mark.parametrize("async_body", [False, True], ids=["sync", "async"])
def test_native_dispatch_rejects_changed_publication(
    gateway_client: TestClient,
    gateway_app: FastAPI,
    signed_headers: Callable,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    change: str,
    async_body: bool,
) -> None:
    """A reload during preparation must stop the old provider body and still close its toolkit."""
    client_id, code = _code(gateway_client, signed_headers("alice"))
    token = _exchange(gateway_client, client_id, code).json()["access_token"]
    paused = threading.Event()
    release = threading.Event()
    events: list[str] = []

    build = _native_dispatch_builder(phase, async_body, paused, release, events)
    monkeypatch.setattr(agents, "build_agent_toolkit", build)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            gateway_client.post,
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "invoke_tool",
                    "arguments": {
                        "toolkit": "calculator",
                        "function": "async_account" if async_body else "account",
                        "arguments": {},
                    },
                },
            },
            headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
        )
        try:
            assert paused.wait(10), "Native dispatch never reached the preparation boundary"
            state = config_lifecycle.require_api_state(gateway_app)
            original = state.snapshot
            assert original.runtime_config is not None
            updated = original.runtime_config.authored_model_dump()
            if change == "remove":
                updated["agents"]["personal"]["tools"] = []
            elif change == "scope":
                updated["agents"]["personal"]["private"]["per"] = "user"
            elif change == "access":
                updated["agents"]["personal"]["access"]["users"] = ["@bob:example.org"]
            config_lifecycle.persist_runtime_validated_config(Config.model_validate(updated), original.runtime_paths)
            assert state.snapshot is not original
        finally:
            release.set()
        response = pending.result(timeout=10)

    assert response.status_code == 200, response.text
    result = response.json()["result"]["structuredContent"]
    if change == "unchanged":
        assert events == ["body", "close"]
        assert result == {"result": "account-result"}
    else:
        assert events == ["close"], "Stale provider body ran after configuration publication"
        assert result == {"error": {"code": "tool_unavailable", "message": "This tool is currently unavailable."}}


def test_public_metadata_challenge_and_bearer_boundary(gateway_client: TestClient, signed_headers: Callable) -> None:
    """External clients can discover OAuth without gaining browser or owner authority."""
    client = gateway_client
    metadata = client.get("/.well-known/oauth-authorization-server/mcp/oauth")
    assert metadata.status_code == 200
    assert metadata.json()["issuer"] == ORIGIN + "/mcp/oauth"
    assert metadata.json()["token_endpoint_auth_methods_supported"] == ["none"]
    resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == RESOURCE
    for response in [_list(client), _list(client, "owner-key"), _list(client, **signed_headers("alice"))]:
        assert response.status_code == 401
        assert 'resource_metadata="' + ORIGIN in response.headers["www-authenticate"]


def test_client_login_pkce_token_refresh_revocation(gateway_client: TestClient, signed_headers: Callable) -> None:
    """A standard public client can consent, use, refresh and revoke its own grant."""
    client = gateway_client
    client_id, code = _code(client, signed_headers("alice"))
    assert _exchange(client, client_id, code, resource="https://other.example.org/mcp").status_code == 400
    assert _exchange(client, client_id, code, code_verifier="wrong").status_code == 400
    response = _exchange(client, client_id, code)
    assert response.status_code == 200, response.text
    tokens = response.json()
    tools = _list(client, tokens["access_token"])
    assert tools.status_code == 200, tools.text
    assert {tool["name"] for tool in tools.json()["result"]["tools"]} == {"search_tools", "get_tool", "invoke_tool"}
    assert "mcp-session-id" not in tools.headers
    assert _exchange(client, client_id, code).status_code == 400
    assert (
        client.post(
            "/mcp/oauth/token",
            data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": tokens["refresh_token"]},
        ).status_code
        == 400
    )
    refreshed = client.post(
        "/mcp/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": tokens["refresh_token"],
            "resource": RESOURCE,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    assert _list(client, tokens["access_token"]).status_code == 401
    access = refreshed.json()["access_token"]
    assert _list(client, access).status_code == 200
    assert client.post("/mcp/oauth/revoke", data={"client_id": client_id, "token": access}).status_code == 200
    assert _list(client, access).status_code == 401


def test_consent_checks_signed_user_origin_and_csrf(gateway_client: TestClient, signed_headers: Callable) -> None:
    """Consent cannot be stolen, forged by another site, or accepted twice."""
    client = gateway_client
    _, url = _authorize(client)
    assert client.get(url).status_code in {401, 403}
    fields = _consent(client, url, signed_headers("alice"))
    assert client.get(url, headers=signed_headers("bob")).status_code == 400
    for headers, data in [
        ({**signed_headers("bob"), "Origin": ORIGIN}, {**fields, "decision": "allow"}),
        ({**signed_headers("alice"), "Origin": "https://evil.example.org"}, {**fields, "decision": "allow"}),
        ({**signed_headers("alice"), "Origin": ORIGIN}, {**fields, "csrf_token": "wrong", "decision": "allow"}),
    ]:
        assert client.post("/connections/mcp/authorize", data=data, headers=headers).status_code in {400, 403}
    headers = {**signed_headers("alice"), "Origin": ORIGIN}
    response = client.post("/connections/mcp/authorize", data={**fields, "decision": "deny"}, headers=headers)
    assert response.status_code == 303
    assert response.headers["location"].startswith(CALLBACK + "&")
    assert "error=access_denied" in response.headers["location"]
    assert (
        client.post("/connections/mcp/authorize", data={**fields, "decision": "allow"}, headers=headers).status_code
        == 400
    )


def test_access_removal_and_alias_reassignment_reject_existing_token(
    gateway_client: TestClient,
    signed_headers: Callable,
    gateway_app: FastAPI,
) -> None:
    """Old OAuth authority cannot survive access removal or change credential owner."""
    client_id, code = _code(gateway_client, signed_headers("alice"))
    token = _exchange(gateway_client, client_id, code).json()["access_token"]
    config = config_lifecycle.require_api_state(gateway_app).snapshot.runtime_config
    assert config is not None
    config.agents["personal"].access.users = ["@bob:example.org"]
    assert _list(gateway_client, token).status_code == 401
    config.authorization.aliases = {"@bob:example.org": ["@alice:example.org"]}
    assert _list(gateway_client, token).status_code == 401


def test_original_signed_alias_must_still_resolve_to_bound_owner(
    gateway_client: TestClient,
    signed_headers: Callable,
    gateway_app: FastAPI,
) -> None:
    """Moving an authenticated bridge identity cannot retain its previous owner's credentials."""
    config = config_lifecycle.require_api_state(gateway_app).snapshot.runtime_config
    assert config is not None
    config.authorization.aliases = {"@alice:example.org": ["@bob:example.org"]}
    client_id, code = _code(gateway_client, signed_headers("bob"))
    token = _exchange(gateway_client, client_id, code).json()["access_token"]
    assert _list(gateway_client, token).status_code == 200
    config.authorization.aliases = {}
    assert _list(gateway_client, token).status_code == 401


def test_consent_cannot_change_signed_identity_even_with_same_canonical_owner(
    gateway_client: TestClient,
    signed_headers: Callable,
    gateway_app: FastAPI,
) -> None:
    """Two signed identities sharing an alias cannot take over each other's consent."""
    config = config_lifecycle.require_api_state(gateway_app).snapshot.runtime_config
    assert config is not None
    config.authorization.aliases = {"@alice:example.org": ["@bob:example.org"]}
    _, url = _authorize(gateway_client)
    _consent(gateway_client, url, signed_headers("bob"))
    assert gateway_client.get(url, headers=signed_headers("alice")).status_code == 400


def test_public_oauth_preflight_does_not_inherit_dashboard_cookie_cors(gateway_client: TestClient) -> None:
    """Browser OAuth clients can reach public machine endpoints without dashboard credentials."""
    for path in ["/mcp/oauth/register", "/mcp/oauth/token", "/mcp/oauth/revoke"]:
        response = gateway_client.options(
            path,
            headers={
                "Origin": "https://client.example.org",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "*"
        assert "access-control-allow-credentials" not in response.headers


def test_configured_browser_origin_can_complete_bearer_mcp_requests(
    gateway_app: FastAPI,
    signed_headers: Callable,
) -> None:
    """Gateway browser CORS and transport Origin validation must agree."""
    snapshot = config_lifecycle.require_api_state(gateway_app).snapshot
    snapshot.runtime_paths = replace(
        snapshot.runtime_paths,
        process_env={
            **snapshot.runtime_paths.process_env,
            "MINDROOM_MCP_GATEWAY_ALLOWED_ORIGINS": "https://client.example.org",
        },
    )
    with TestClient(gateway_app, base_url=ORIGIN, follow_redirects=False) as client:
        response = client.options(
            "/mcp",
            headers={
                "Origin": "https://client.example.org",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,authorization,mcp-protocol-version",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "https://client.example.org"
        assert "access-control-allow-credentials" not in response.headers
        client_id, code = _code(client, signed_headers("alice"))
        token = _exchange(client, client_id, code).json()["access_token"]
        response = _list(client, token, Origin="https://client.example.org")
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "https://client.example.org"
        assert "www-authenticate" in response.headers["access-control-expose-headers"].lower()
        assert _list(client, token, Origin="https://evil.example.org").status_code == 403


def test_onboarding_rate_limit_preserves_existing_grants_and_recovers(
    gateway_app: FastAPI,
    signed_headers: Callable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public onboarding has bounded admission without blocking current client grants."""
    now = [100.0]
    monkeypatch.setattr(admission, "monotonic", lambda: now[0])
    snapshot = config_lifecycle.require_api_state(gateway_app).snapshot
    snapshot.runtime_paths = replace(
        snapshot.runtime_paths,
        process_env={**snapshot.runtime_paths.process_env, "MINDROOM_MCP_GATEWAY_ONBOARDING_RATE_LIMIT": "2"},
    )
    with TestClient(gateway_app, base_url=ORIGIN, follow_redirects=False) as client:
        client_id, code = _code(client, signed_headers("alice"))
        for response in [
            client.post("/mcp/oauth/register", json={}),
            client.get("/mcp/oauth/authorize"),
        ]:
            assert response.status_code == 429
            assert response.json() == {"error": "slow_down"}
            assert response.headers["retry-after"] == "60"
            assert "no-store" in response.headers["cache-control"]
        assert client.get("/.well-known/oauth-protected-resource/mcp").status_code == 200
        tokens = _exchange(client, client_id, code).json()
        assert _list(client, tokens["access_token"]).status_code == 200
        refreshed = client.post(
            "/mcp/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": tokens["refresh_token"],
                "resource": RESOURCE,
            },
        )
        assert refreshed.status_code == 200
        access = refreshed.json()["access_token"]
        assert client.post("/mcp/oauth/revoke", data={"client_id": client_id, "token": access}).status_code == 200
        assert _list(client, access).status_code == 401
        now[0] += 60
        _authorize(client)


def test_onboarding_rate_limit_is_fair_across_request_sources(gateway_app: FastAPI) -> None:
    """One source cannot consume another source's onboarding allowance."""
    _set_onboarding_limits(gateway_app, aggregate=6, source=2)
    with TestClient(
        gateway_app,
        base_url=ORIGIN,
        follow_redirects=False,
        client=("192.0.2.10", 41000),
    ) as client:
        _authorize(client)

        _set_peer(client, "192.0.2.10", 41001)
        denied = client.post(
            "/mcp/oauth/register",
            json={},
            headers={"Forwarded": "for=192.0.2.20", "X-Forwarded-For": "192.0.2.20"},
        )
        assert denied.status_code == 429

        _set_peer(client, "::ffff:192.0.2.10", 41002)
        denied = client.get(
            "/mcp/oauth/authorize",
            headers={"Forwarded": "for=192.0.2.30", "X-Real-IP": "192.0.2.30"},
        )
        assert denied.status_code == 429

        _set_peer(client, "192.0.2.20", 42000)
        _authorize(client)


def test_invalid_onboarding_consumes_source_allowance_before_oauth_validation(
    gateway_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admitted malformed requests cannot bypass source limits by failing validation."""
    _set_onboarding_limits(gateway_app, aggregate=6, source=2)
    with TestClient(
        gateway_app,
        base_url=ORIGIN,
        follow_redirects=False,
        client=("192.0.2.10", 41000),
    ) as client:
        runtime = config_lifecycle.app_state(gateway_app).mcp_gateway_runtime
        assert runtime is not None
        calls = 0
        original_handle = runtime.register.handle

        async def tracked_handle(request: Request) -> Response:
            nonlocal calls
            calls += 1
            return await original_handle(request)

        monkeypatch.setattr(runtime.register, "handle", tracked_handle)
        for _ in range(2):
            response = client.post("/mcp/oauth/register", json={})
            assert response.status_code == 400

        rejected = client.post("/mcp/oauth/register", json={})
        assert rejected.status_code == 429
        assert rejected.json() == {"error": "slow_down"}
        assert calls == 2

        _set_peer(client, "192.0.2.20", 42000)
        _, redirect = _authorize(client)
        assert redirect.startswith(ORIGIN + "/connections/mcp/authorize?")


def test_onboarding_aggregate_limit_applies_across_distinct_sources(gateway_app: FastAPI) -> None:
    """Distinct sources remain subject to the bounded aggregate allowance."""
    _set_onboarding_limits(gateway_app, aggregate=6, source=2)
    with TestClient(gateway_app, base_url=ORIGIN, follow_redirects=False) as client:
        for last_octet in range(1, 4):
            _set_peer(client, f"192.0.2.{last_octet}")
            _authorize(client)

        _set_peer(client, "192.0.2.4")
        denied = client.post("/mcp/oauth/register", json={})
        assert denied.status_code == 429
        assert denied.json() == {"error": "slow_down"}


def test_onboarding_unknown_source_groups_and_recovers_at_boundary(
    gateway_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing and invalid peers share one allowance that expires at the window boundary."""
    now = [100.0]
    monkeypatch.setattr(admission, "monotonic", lambda: now[0])
    _set_onboarding_limits(gateway_app, aggregate=6, source=2)
    with TestClient(gateway_app, base_url=ORIGIN, follow_redirects=False, client=None) as client:
        _authorize(client)

        _set_peer(client, "not-an-ip", 51000)
        assert client.post("/mcp/oauth/register", json={}).status_code == 429
        now[0] = 159.999
        assert client.get("/mcp/oauth/authorize").status_code == 429

        now[0] = 160.0
        _authorize(client)


@pytest.mark.parametrize("operation", ["register", "authorize"])
def test_onboarding_storage_capacity_returns_protocol_backpressure(gateway_app: FastAPI, operation: str) -> None:
    """Public storage exhaustion returns a controlled response through the real SDK handlers."""
    snapshot = config_lifecycle.require_api_state(gateway_app).snapshot
    snapshot.runtime_paths = replace(
        snapshot.runtime_paths,
        process_env={
            **snapshot.runtime_paths.process_env,
            "MINDROOM_MCP_GATEWAY_ONBOARDING_MAX_BYTES": "1024" if operation == "register" else "2048",
        },
    )
    with TestClient(gateway_app, base_url=ORIGIN, follow_redirects=False) as client:
        if operation == "authorize":
            _, callback = _authorize(client)
            assert callback.startswith(CALLBACK + "&")
            assert parse_qs(urlsplit(callback).query)["error"] == ["temporarily_unavailable"]
        else:
            response = client.post(
                "/mcp/oauth/register",
                json={
                    "redirect_uris": [CALLBACK],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                },
            )
            assert response.status_code == 503
            assert response.json() == {"error": "temporarily_unavailable"}
            assert response.headers["retry-after"] == "60"
            assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("operation", ["binding", "approval"])
def test_consent_storage_capacity_is_private_and_preserves_nonce(
    gateway_app: FastAPI,
    gateway_client: TestClient,
    signed_headers: Callable,
    operation: str,
) -> None:
    """Both browser mutations return bounded backpressure and leave the original consent usable."""
    _, url = _authorize(gateway_client)
    headers = signed_headers("alice")
    fields = _consent(gateway_client, url, headers)
    runtime = config_lifecycle.app_state(gateway_app).mcp_gateway_runtime
    assert runtime is not None
    budget = runtime.provider.store.max_bytes
    runtime.provider.store.max_bytes = 1
    if operation == "binding":
        response = gateway_client.get(url, headers=headers)
    else:
        response = gateway_client.post(
            "/connections/mcp/authorize",
            data={**fields, "decision": "allow"},
            headers={**headers, "Origin": ORIGIN},
        )
    assert response.status_code == 503
    assert response.json() == {"error": "temporarily_unavailable"}
    assert response.headers["retry-after"] == "60"
    assert "no-store" in response.headers["cache-control"]
    assert len(response.content) < 100
    runtime.provider.store.max_bytes = budget
    approved = gateway_client.post(
        "/connections/mcp/authorize",
        data={**fields, "decision": "allow"},
        headers={**headers, "Origin": ORIGIN},
    )
    assert approved.status_code == 303


def test_token_storage_capacity_preserves_authorization_code(
    gateway_app: FastAPI,
    gateway_client: TestClient,
    signed_headers: Callable,
) -> None:
    """SDK token exchange propagates capacity and can retry the same valid code."""
    client_id, code = _code(gateway_client, signed_headers("alice"))
    runtime = config_lifecycle.app_state(gateway_app).mcp_gateway_runtime
    assert runtime is not None
    budget = runtime.provider.store.max_bytes
    runtime.provider.store.max_bytes = 1
    response = _exchange(gateway_client, client_id, code)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"
    assert "no-store" in response.headers["cache-control"]
    runtime.provider.store.max_bytes = budget
    assert _exchange(gateway_client, client_id, code).status_code == 200


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("MINDROOM_MCP_GATEWAY_ENABLED", "false"),
        ("MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT", "false"),
        ("MINDROOM_PUBLIC_URL", "https://example.org/unexpected-path"),
        ("MINDROOM_MCP_GATEWAY_ALLOWED_ORIGINS", "*"),
        ("MINDROOM_MCP_GATEWAY_ONBOARDING_RATE_LIMIT", "0"),
        ("MINDROOM_MCP_GATEWAY_ONBOARDING_RATE_LIMIT", "invalid"),
        ("MINDROOM_MCP_GATEWAY_ONBOARDING_SOURCE_RATE_LIMIT", "0"),
        ("MINDROOM_MCP_GATEWAY_ONBOARDING_SOURCE_RATE_LIMIT", "invalid"),
        ("MINDROOM_MCP_GATEWAY_ONBOARDING_MAX_BYTES", "0"),
        ("MINDROOM_MCP_GATEWAY_ONBOARDING_MAX_BYTES", "invalid"),
        *[
            (setting, value)
            for setting in (
                "MINDROOM_MCP_GATEWAY_MAX_ACTIVE_CALLS",
                "MINDROOM_MCP_GATEWAY_MAX_USER_CALLS",
                "MINDROOM_MCP_GATEWAY_MAX_GRANT_CALLS",
            )
            for value in ("0", "-1", "invalid", "1.5")
        ],
        ("MINDROOM_MCP_OAUTH_MAX_BYTES", "0"),
        ("MINDROOM_MCP_OAUTH_MAX_BYTES", "invalid"),
        ("MINDROOM_MCP_OAUTH_USER_MAX_BYTES", "0"),
        ("MINDROOM_MCP_OAUTH_USER_MAX_BYTES", "invalid"),
    ],
)
def test_disabled_or_invalid_gateway_does_not_start(gateway_app: FastAPI, setting: str, value: str) -> None:
    """An opt-in or auth configuration error cannot expose a partial gateway."""
    snapshot = config_lifecycle.require_api_state(gateway_app).snapshot
    snapshot.runtime_paths = replace(
        snapshot.runtime_paths,
        process_env={**snapshot.runtime_paths.process_env, setting: value},
    )
    with TestClient(gateway_app, base_url=ORIGIN) as client:
        assert _list(client).status_code == 404
        assert client.get("/.well-known/oauth-authorization-server/mcp/oauth").status_code == 404


@pytest.mark.parametrize("body", [b'{"client_name":', b'{"client_name":"\xff"}', b"[" * 1100])
def test_registration_parse_errors_are_private_invalid_requests(gateway_client: TestClient, body: bytes) -> None:
    """Malformed JSON, UTF-8 and excessive nesting fail at the bounded body boundary."""
    response = gateway_client.post("/mcp/oauth/register", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}
    assert "no-store" in response.headers["cache-control"]


def test_registration_oversized_integer_is_private_invalid_request(gateway_app: FastAPI) -> None:
    """Integer conversion limits must produce the same private response as other JSON parse failures."""
    marker = "harmless-registration-integer-marker"
    body = b'{"client_name":"' + marker.encode() + b'","value":' + b"1" * 5000 + b"}"
    assert len(body) < 131072
    with TestClient(gateway_app, base_url=ORIGIN, raise_server_exceptions=False) as client:
        response = client.post("/mcp/oauth/register", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}
    assert "no-store" in response.headers["cache-control"]
    assert marker not in response.text
    assert "1" * 5000 not in response.text


@pytest.mark.parametrize(
    ("setting", "other_grant_admitted", "other_user_admitted"),
    [
        ("MINDROOM_MCP_GATEWAY_MAX_ACTIVE_CALLS", False, False),
        ("MINDROOM_MCP_GATEWAY_MAX_USER_CALLS", False, True),
        ("MINDROOM_MCP_GATEWAY_MAX_GRANT_CALLS", True, True),
    ],
)
def test_configured_call_limits_reach_http_admission(
    gateway_app: FastAPI,
    signed_headers: Callable[[str], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    other_grant_admitted: bool,
    other_user_admitted: bool,
) -> None:
    """Startup settings constrain the intended scope across real OAuth grants."""
    snapshot = config_lifecycle.require_api_state(gateway_app).snapshot
    snapshot.runtime_paths = replace(
        snapshot.runtime_paths,
        process_env={**snapshot.runtime_paths.process_env, setting: "1"},
    )
    started = threading.Event()
    release = threading.Event()

    async def dispatch(
        _runtime: GatewayRuntime,
        _request: Request,
        _name: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        if arguments.get("query") == "hold":
            started.set()
            assert await asyncio.to_thread(release.wait, 10), "Call was never released"
        return {"tools": []}

    monkeypatch.setattr(GatewayRuntime, "dispatch", dispatch)
    with (
        TestClient(gateway_app, base_url=ORIGIN, follow_redirects=False) as client,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        tokens = [
            _exchange(client, *_code(client, signed_headers(user))).json()["access_token"]
            for user in ("alice", "alice", "bob")
        ]

        def call(token: str, request_id: int, query: str = "probe") -> httpx.Response:
            return client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": "search_tools", "arguments": {"query": query}},
                },
                headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
            )

        pending = executor.submit(call, tokens[0], 1, "hold")
        try:
            assert started.wait(10), "Call never reached dispatch"
            for token, admitted in zip(tokens, (False, other_grant_admitted, other_user_admitted), strict=True):
                result = call(token, 2).json()["result"]
                if admitted:
                    assert result["structuredContent"] == {"tools": []}
                else:
                    assert result["isError"] is True
                    assert result["structuredContent"]["error"]["code"] == "busy"
        finally:
            release.set()
            assert pending.result(timeout=10).json()["result"]["isError"] is False
        assert call(tokens[0], 3).json()["result"]["structuredContent"] == {"tools": []}
