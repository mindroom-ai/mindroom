"""External signed credentials use real SCIM, MCP transport, and personal tools."""

# Shared gateway fixtures are intentionally shadowed by pytest injection.
# ruff: noqa: F811

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import TYPE_CHECKING

import jwt
import pytest
from agno.tools import Toolkit
from fastapi.testclient import TestClient

from mindroom import agents
from mindroom.api import config_lifecycle
from mindroom.tool_system.worker_routing import get_tool_execution_identity
from tests.api.test_api import _trusted_upstream_jwks, _trusted_upstream_jwt_key
from tests.api.test_mcp_gateway_api import (
    MCP_HEADERS,
    ORIGIN,
    RESOURCE,
    _code,
    _exchange,
    gateway_app,  # noqa: F401
    gateway_client,  # noqa: F401
    signed_headers,  # noqa: F401
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    import httpx
    from fastapi import FastAPI
    from starlette.requests import Request

    from mindroom.mcp_gateway.oauth import GatewayAccessToken
    from mindroom.mcp_gateway.types import GatewayPrincipal

SCIM = "/mcp/scim/v2/Users"
SCIM_HEADERS = {"Authorization": "Bearer synthetic-provisioning-secret-for-local-tests"}
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
PROFILES = [
    {"issuer": "https://identity.example.org/", "server": "https://identity.example.org/", "header": "authorization"},
    {"issuer": "https://edge.example.org", "server": "https://login.example.org/oauth", "header": "x-access-assertion"},
]


@pytest.fixture(params=PROFILES, ids=["bearer", "signed-assertion"])
def profile(request: pytest.FixtureRequest) -> dict[str, str]:
    """Exercise distinct OAuth issuers and origin assertion issuers."""
    return request.param


def _settings(app: FastAPI, **updates: str) -> None:
    snapshot = config_lifecycle.require_api_state(app).snapshot
    snapshot.runtime_paths = replace(
        snapshot.runtime_paths,
        process_env={**snapshot.runtime_paths.process_env, **updates},
    )


@pytest.fixture
def external_app(gateway_app: FastAPI, profile: dict[str, str]) -> FastAPI:
    """Configure the external authority before starting the common runtime."""
    _settings(
        gateway_app,
        MINDROOM_MCP_AUTH_MODE="external",
        MINDROOM_MCP_SCIM_TOKEN="synthetic-provisioning-secret-for-local-tests",  # noqa: S106
        MINDROOM_MCP_EXTERNAL_AUTHORIZATION_SERVER=profile["server"],
        MINDROOM_MCP_EXTERNAL_ISSUER=profile["issuer"],
        MINDROOM_MCP_EXTERNAL_AUDIENCE=RESOURCE,
        MINDROOM_MCP_EXTERNAL_CLIENT_ID="registered-client" if profile["header"] == "authorization" else "",
        MINDROOM_MCP_EXTERNAL_JWKS_URL=profile["issuer"].rstrip("/") + "/jwks",
        MINDROOM_MCP_EXTERNAL_TOKEN_HEADER=profile["header"],
        MINDROOM_MCP_EXTERNAL_EMAIL_CLAIM="sub" if profile["header"] == "authorization" else "email",
        MINDROOM_MCP_EXTERNAL_EMAIL_DOMAIN="example.org" if profile["header"] == "authorization" else "",
        MINDROOM_MCP_EXTERNAL_EMAIL_TO_MATRIX_USER_ID_TEMPLATE="@{localpart}:example.org"
        if profile["header"] == "authorization"
        else "",
        MINDROOM_MCP_EXTERNAL_MATRIX_USER_ID_CLAIM="matrix_user_id" if profile["header"] != "authorization" else "",
        MINDROOM_MCP_EXTERNAL_REQUIRED_SCOPES="mcp:tools",
    )
    return gateway_app


@pytest.fixture
def external_client(external_app: FastAPI) -> Iterator[TestClient]:
    """Run real production routes, SDK and durable account storage."""
    with TestClient(external_app, base_url=ORIGIN, follow_redirects=False) as client:
        yield client


@pytest.fixture
def credential(profile: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Callable[..., dict[str, str]]:
    """Sign real JWTs; only replace the remote JWKS wire response."""
    key = _trusted_upstream_jwt_key()
    keys = _trusted_upstream_jwks(key)
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _client: keys)

    def headers(user: str = "alice", **claims: object) -> dict[str, str]:
        token = jwt.encode(
            {
                "iss": profile["issuer"],
                "aud": RESOURCE,
                "sub": f"{user}@example.org",
                "email": f"{user}@example.org",
                "matrix_user_id": f"@{user}:example.org",
                "iat": time.time(),
                "exp": time.time() + 300,
                "scope": "mcp:tools",
                **({"client_id": "registered-client"} if profile["header"] == "authorization" else {}),
                **claims,
            },
            key,
            algorithm="RS256",
            headers={"kid": keys["keys"][0]["kid"]},
        )
        return {profile["header"]: f"Bearer {token}" if profile["header"] == "authorization" else token}

    return headers


def _provision(client: TestClient, user: str) -> str:
    response = client.post(
        SCIM,
        headers=SCIM_HEADERS,
        json={"schemas": [USER_SCHEMA], "userName": f"{user}@example.org", "active": True},
    )
    assert response.status_code == 201, response.text
    return SCIM + "/" + response.json()["id"]


def _list(client: TestClient, headers: dict[str, str]) -> httpx.Response:
    return client.post(
        "/mcp",
        headers={**MCP_HEADERS, **headers},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )


def _active(client: TestClient, path: str, *, active: bool) -> None:
    response = client.patch(
        path,
        headers=SCIM_HEADERS,
        json={"schemas": [PATCH_SCHEMA], "Operations": [{"op": "replace", "path": "active", "value": active}]},
    )
    assert response.status_code == 200, response.text


def test_external_metadata_and_scim_offboarding(
    external_client: TestClient,
    profile: dict[str, str],
    credential: Callable[..., dict[str, str]],
) -> None:
    """External discovery and committed SCIM changes govern every user's next MCP request."""
    client = external_client
    metadata = client.get("/.well-known/oauth-protected-resource/mcp")
    assert metadata.status_code == 200
    assert metadata.json() == {
        "resource": RESOURCE,
        "authorization_servers": [profile["server"]],
        "scopes_supported": ["mcp:tools"],
        "bearer_methods_supported": ["header"],
    }
    assert _list(client, credential()).status_code == 401
    alice_path = _provision(client, "alice")
    _provision(client, "bob")
    alice, bob = credential("alice"), credential("bob")
    for headers in (alice, bob):
        response = _list(client, headers)
        assert response.status_code == 200, response.text
        assert {tool["name"] for tool in response.json()["result"]["tools"]} == {
            "search_tools",
            "get_tool",
            "invoke_tool",
        }
    profile_update = client.patch(
        alice_path,
        headers=SCIM_HEADERS,
        json={
            "schemas": [PATCH_SCHEMA],
            "Operations": [{"op": "replace", "path": "displayName", "value": "Alice Updated"}],
        },
    )
    assert profile_update.status_code == 200
    assert _list(client, alice).status_code == 200
    _active(client, alice_path, active=False)
    assert _list(client, alice).status_code == 401
    assert _list(client, bob).status_code == 200
    _active(client, alice_path, active=True)
    assert _list(client, alice).status_code == 401
    assert _list(client, credential()).status_code == 200
    assert _list(client, bob).status_code == 200


def test_external_cancellation_and_user_limit_span_tokens(
    external_app: FastAPI,
    credential: Callable[..., dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another user or renewed JWT cannot cancel a call or bypass the shared user allowance."""
    _settings(external_app, MINDROOM_MCP_GATEWAY_MAX_USER_CALLS="1")
    started, release = threading.Event(), threading.Event()

    async def account() -> str:
        current = get_tool_execution_identity()
        assert current is not None
        if current.requester_id == "@alice:example.org":
            started.set()
            await asyncio.to_thread(release.wait)
        return current.requester_id

    monkeypatch.setattr(
        agents,
        "build_agent_toolkit",
        lambda *_args, **_kwargs: Toolkit(name="calculator", tools=[account]),
    )
    call = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {
            "name": "invoke_tool",
            "arguments": {"toolkit": "calculator", "function": "account", "arguments": {}},
        },
    }
    cancel = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}}
    with TestClient(external_app, base_url=ORIGIN) as client, ThreadPoolExecutor(max_workers=1) as executor:
        _provision(client, "alice")
        _provision(client, "bob")
        alice, renewed, bob = credential(), credential(), credential("bob")
        assert alice != renewed
        pending = executor.submit(client.post, "/mcp", json=call, headers={**MCP_HEADERS, **alice})
        try:
            assert started.wait(10), "Native tool never started"
            for headers in (bob, renewed):
                assert client.post("/mcp", json=cancel, headers={**MCP_HEADERS, **headers}).status_code == 202
            busy = client.post("/mcp", json={**call, "id": 8}, headers={**MCP_HEADERS, **renewed})
            assert busy.json()["result"]["structuredContent"]["error"]["code"] == "busy"
            assert not pending.done()
            other = client.post("/mcp", json=call, headers={**MCP_HEADERS, **bob})
            assert other.json()["result"]["structuredContent"] == {"result": "@bob:example.org"}
            assert client.post("/mcp", json=cancel, headers={**MCP_HEADERS, **alice}).status_code == 202
            assert pending.result(timeout=10).json()["result"]["structuredContent"]["error"]["code"] == "cancelled"
        finally:
            release.set()


def test_external_native_tools_keep_requester_identity(
    external_client: TestClient,
    credential: Callable[..., dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real native dispatch carries signed owner identity despite spoofed unsigned headers."""

    def identity() -> str:
        current = get_tool_execution_identity()
        assert current is not None
        assert current.agent_name == "personal"
        return current.requester_id

    monkeypatch.setattr(
        agents,
        "build_agent_toolkit",
        lambda *_args, **_kwargs: Toolkit(name="calculator", tools=[identity]),
    )
    for user in ("alice", "bob"):
        _provision(external_client, user)
        response = external_client.post(
            "/mcp",
            headers={
                **MCP_HEADERS,
                **credential(user),
                "X-Trusted-User": "mallory",
                "X-Trusted-Email": "mallory@example.org",
            },
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "invoke_tool",
                    "arguments": {"toolkit": "calculator", "function": "identity", "arguments": {}},
                },
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"]["structuredContent"] == {"result": f"@{user}:example.org"}


@pytest.mark.parametrize("change", ["account", "issuer", "mode"])
def test_external_security_change_between_admission_and_dispatch(
    external_app: FastAPI,
    external_client: TestClient,
    credential: Callable[..., dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """A committed account or authority change after HTTP admission stops provider execution."""
    path = _provision(external_client, "alice")
    runtime = config_lifecycle.app_state(external_app).mcp_gateway_runtime
    assert runtime is not None
    authenticate = runtime.authenticate
    executed: list[str] = []

    async def disable_after_admission(request: Request) -> GatewayPrincipal:
        principal = await authenticate(request)
        if change == "account":
            await asyncio.to_thread(_active, external_client, path, active=False)
        elif change == "mode":
            _settings(external_app, MINDROOM_MCP_AUTH_MODE="builtin")
        else:
            _settings(external_app, MINDROOM_MCP_EXTERNAL_ISSUER="https://changed.example.org")
        return principal

    def forbidden_tool() -> str:
        executed.append("provider body")
        return "unexpected result"

    monkeypatch.setattr(runtime.server, "_authenticate", disable_after_admission)
    monkeypatch.setattr(
        agents,
        "build_agent_toolkit",
        lambda *_args, **_kwargs: Toolkit(name="calculator", tools=[forbidden_tool]),
    )
    response = external_client.post(
        "/mcp",
        headers={**MCP_HEADERS, **credential()},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "invoke_tool",
                "arguments": {"toolkit": "calculator", "function": "forbidden_tool", "arguments": {}},
            },
        },
    )
    assert response.status_code == 200
    assert not executed, "Provider body ran after a committed security change"
    assert response.json()["result"]["structuredContent"]["error"]["code"] == "tool_unavailable"


def _assert_native_call_denied(client: TestClient, headers: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    executed: list[str] = []

    def account() -> str:
        executed.append("provider body")
        return "unexpected result"

    monkeypatch.setattr(
        agents,
        "build_agent_toolkit",
        lambda *_args, **_kwargs: Toolkit(name="calculator", tools=[account]),
    )
    response = client.post(
        "/mcp",
        headers={**MCP_HEADERS, **headers},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "invoke_tool",
                "arguments": {"toolkit": "calculator", "function": "account", "arguments": {}},
            },
        },
    )
    assert response.status_code == 200
    assert not executed, "Provider body ran after a committed security change"
    assert response.json()["result"]["structuredContent"]["error"]["code"] == "tool_unavailable"


@pytest.mark.parametrize("phase", ["verify", "account"])
@pytest.mark.parametrize("change", ["issuer", "mode"])
def test_external_setting_change_during_dispatch_lookup(
    external_app: FastAPI,
    external_client: TestClient,
    credential: Callable[..., dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    change: str,
) -> None:
    """Authority changes during dispatch awaits cannot bind old credentials to a new snapshot."""
    _provision(external_client, "alice")
    runtime = config_lifecycle.app_state(external_app).mcp_gateway_runtime
    assert runtime is not None
    assert runtime.external_auth is not None
    target = runtime.external_auth if phase == "verify" else runtime.accounts
    method = "verify" if phase == "verify" else "resolve_external"
    lookup = getattr(target, method)
    calls = 0

    async def change_after_lookup(*args: object) -> object:
        nonlocal calls
        result = await lookup(*args)
        calls += 1
        if calls == 2:
            state = config_lifecycle.require_api_state(external_app)
            with state.config_lock:
                snapshot = state.snapshot
                updates = (
                    {"MINDROOM_MCP_EXTERNAL_ISSUER": "https://changed.example.org"}
                    if change == "issuer"
                    else {"MINDROOM_MCP_AUTH_MODE": "builtin"}
                )
                paths = replace(snapshot.runtime_paths, process_env={**snapshot.runtime_paths.process_env, **updates})
                state.snapshot = replace(snapshot, runtime_paths=paths, revision=snapshot.revision + 1)
        return result

    monkeypatch.setattr(target, method, change_after_lookup)
    _assert_native_call_denied(external_client, credential(), monkeypatch)
    assert calls == 2


def test_builtin_mode_change_during_dispatch_token_lookup(
    gateway_app: FastAPI,
    gateway_client: TestClient,
    signed_headers: Callable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Built-in grants also fail closed when authentication mode changes during token lookup."""
    client_id, code = _code(gateway_client, signed_headers("alice"))
    token = _exchange(gateway_client, client_id, code).json()["access_token"]
    runtime = config_lifecycle.app_state(gateway_app).mcp_gateway_runtime
    assert runtime is not None
    lookup = runtime.provider.load_access_token
    calls = 0

    async def change_after_lookup(raw: str) -> GatewayAccessToken | None:
        nonlocal calls
        result = await lookup(raw)
        calls += 1
        if calls == 2:
            _settings(gateway_app, MINDROOM_MCP_AUTH_MODE="unknown")
        return result

    monkeypatch.setattr(runtime.provider, "load_access_token", change_after_lookup)
    _assert_native_call_denied(gateway_client, {"Authorization": f"Bearer {token}"}, monkeypatch)


def test_external_credentials_do_not_fall_back(
    external_client: TestClient,
    credential: Callable[..., dict[str, str]],
    profile: dict[str, str],
) -> None:
    """Owner, browser, wrong audience, and wrong token transport cannot acquire MCP authority."""
    _provision(external_client, "alice")
    valid = credential()
    assert _list(external_client, valid).status_code == 200
    raw = next(iter(valid.values())).removeprefix("Bearer ")
    wrong_transport = (
        {"x-access-assertion": raw} if profile["header"] == "authorization" else {"Authorization": f"Bearer {raw}"}
    )
    for headers in (
        {},
        {"Authorization": "Bearer owner-key"},
        {"Cookie": "session=alice"},
        credential(aud="mindroom-dashboard"),
        credential(aud="https://other.example.org/mcp"),
        wrong_transport,
        {"X-Trusted-User": "alice", "X-Trusted-Email": "alice@example.org"},
    ):
        response = _list(external_client, headers)
        assert response.status_code == 401, response.text
        assert (
            f'resource_metadata="{ORIGIN}/.well-known/oauth-protected-resource/mcp"'
            in response.headers["www-authenticate"]
        )
    denied = _list(external_client, credential(scope="profile"))
    assert denied.status_code == 403
    assert 'error="insufficient_scope"' in denied.headers["www-authenticate"]
    assert 'scope="mcp:tools"' in denied.headers["www-authenticate"]


def test_external_client_pin_isolates_shared_issuer(
    external_client: TestClient,
    credential: Callable[..., dict[str, str]],
    profile: dict[str, str],
) -> None:
    """Another signed client cannot impersonate a provisioned user through the pinned bearer profile."""
    _provision(external_client, "alice")
    assert _list(external_client, credential()).status_code == 200
    other = credential(client_id="other-client", aud=[RESOURCE, "other-client"])
    response = _list(external_client, other)
    assert response.status_code == (401 if profile["header"] == "authorization" else 200)


def test_external_disables_local_issuer_and_client_controls(
    external_client: TestClient,
    signed_headers: Callable,
) -> None:
    """External mode cannot register, issue, consent to, or manage local client grants."""
    for path in (
        "/.well-known/oauth-authorization-server/mcp/oauth",
        "/mcp/oauth/.well-known/oauth-authorization-server",
        "/mcp/oauth/authorize",
        "/connections/mcp/authorize",
    ):
        assert external_client.get(path, headers=signed_headers("alice")).status_code == 404
    for path in (
        "/mcp/oauth/register",
        "/mcp/oauth/token",
        "/mcp/oauth/revoke",
        "/mcp/oauth/authorize",
        "/connections/mcp/authorize",
    ):
        assert external_client.post(path, json={}, headers=signed_headers("alice")).status_code == 404
    clients = "/api/connections/mcp/clients"
    assert external_client.get(clients, headers=signed_headers("alice")).json() == {"enabled": False, "clients": []}
    for suffix in ("/revoke-all", "/unknown/revoke"):
        assert (
            external_client.post(
                clients + suffix,
                json={},
                headers={**signed_headers("alice"), "Origin": ORIGIN},
            ).status_code
            == 404
        )


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("MINDROOM_MCP_AUTH_MODE", "builtin"),
        ("MINDROOM_MCP_AUTH_MODE", "unknown"),
        ("MINDROOM_MCP_EXTERNAL_ISSUER", "https://changed.example.org"),
        ("MINDROOM_MCP_EXTERNAL_AUDIENCE", "other-resource"),
        ("MINDROOM_MCP_EXTERNAL_CLIENT_ID", "changed-client"),
        ("MINDROOM_MCP_EXTERNAL_JWKS_URL", ""),
        ("MINDROOM_MCP_SCIM_TOKEN", ""),
    ],
)
def test_external_setting_changes_fail_closed(
    external_app: FastAPI,
    external_client: TestClient,
    credential: Callable[..., dict[str, str]],
    setting: str,
    value: str,
) -> None:
    """Hot configuration changes cannot silently weaken or replace the pinned authority."""
    _provision(external_client, "alice")
    headers = credential()
    assert _list(external_client, headers).status_code == 200
    _settings(external_app, **{setting: value})
    assert _list(external_client, headers).status_code == 404
    assert external_client.get("/.well-known/oauth-protected-resource/mcp").status_code == 404


def test_external_requires_scim_at_startup(external_app: FastAPI) -> None:
    """External mode never starts with an optional or absent account gate."""
    _settings(external_app, MINDROOM_MCP_SCIM_TOKEN="")
    with TestClient(external_app, base_url=ORIGIN) as client:
        assert client.get("/.well-known/oauth-protected-resource/mcp").status_code == 404
