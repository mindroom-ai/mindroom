"""Connections portal sign-in: a Matrix OpenID token becomes a portal session cookie."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from aiohttp import web
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from mindroom.api import auth, config_lifecycle, connections_session, frontend, main
from mindroom.api.connections_sessions import CONNECTIONS_SESSION_COOKIE, ConnectionsSessionStore
from mindroom.matrix_openid import MatrixOpenIDError, verify_matrix_openid
from mindroom.oauth import registry as oauth_registry
from tests.api.test_api import (
    _trusted_upstream_jwks,
    _trusted_upstream_jwt,
    _trusted_upstream_jwt_key,
    _trusted_upstream_strict_jwt_env,
)
from tests.api.test_oauth_api import (
    _fake_provider,
    _publish_config,
    _runtime_paths,
    _stored_oauth_credentials,
    _use_runtime_auth_settings,
)
from tests.computer_helpers import ComputerPeer

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterator
    from pathlib import Path

    from httpx import Response

PORTAL_ORIGIN = "https://portal.example.org"
CHAT_ORIGIN = "https://chat.example.org"


@pytest.fixture
def signin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enforce_turn_authorization: None) -> dict[str, Any]:  # noqa: ARG001
    """Serve the real API in API-key mode with a fake Matrix OpenID verifier for users alice and bob."""

    async def verify(token: Any, _paths: Any) -> str:  # noqa: ANN401
        if token.access_token in {"alice", "bob"}:
            return f"@{token.access_token}:example.org"
        raise MatrixOpenIDError(401, "Matrix OpenID verification failed.")

    verifier = AsyncMock(side_effect=verify)
    monkeypatch.setattr(connections_session, "verify_matrix_openid", verifier)
    monkeypatch.setattr(
        "mindroom.server_fetch_url.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(0, 0, 0, "", ("93.184.216.34", 0))],
    )
    provider = _fake_provider(provider_id="google_drive", credential_service="google_drive_oauth")
    monkeypatch.setattr(oauth_registry, "_builtin_oauth_providers", lambda: (provider,))
    env = {
        "MINDROOM_API_KEY": "dashboard-key",
        "MINDROOM_OWNER_USER_ID": "@owner:example.org",
        "MINDROOM_CONNECTIONS_AGENT": "personal",
        "MINDROOM_CONNECTIONS_ALLOWED_ORIGINS": f'["{CHAT_ORIGIN}"]',
        "MINDROOM_PUBLIC_URL": PORTAL_ORIGIN,
        "TEST_OAUTH_CLIENT_ID": "test-client",
        "TEST_OAUTH_CLIENT_SECRET": "test-secret",
    }
    paths = _runtime_paths(tmp_path, env)
    payload = {
        "models": {"default": {"provider": "ollama", "id": "test-model"}},
        "agents": {
            "personal": {
                "display_name": "Personal Mind",
                "role": "Personal assistant",
                "tools": ["calculator", {"name": "google_drive", "defer": True}],
                "private": {"per": "user_agent"},
                "access": {"users": ["@alice:example.org", "@bob:example.org"]},
            },
        },
    }
    main.initialize_api_app(main.app, paths)
    _publish_config(main.app, paths, payload)
    _use_runtime_auth_settings(main.app)
    return {
        "client": TestClient(main.app, base_url=PORTAL_ORIGIN),
        "paths": paths,
        "payload": payload,
        "provider": provider,
        "verifier": verifier,
    }


def sign_in(
    client: TestClient,
    name: str,
    origin: str = CHAT_ORIGIN,
    headers: dict[str, str] | None = None,
) -> Response:
    """Post a Matrix OpenID token for `name` the way the portal page does."""
    return client.post(
        "/api/connections/session",
        headers={"Origin": PORTAL_ORIGIN, **(headers or {})},
        json={
            "openid_token": {
                "access_token": name,
                "token_type": "Bearer",
                "matrix_server_name": "example.org",
                "expires_in": 300,
            },
            "client_origin": origin,
        },
    )


def _serve(signin: dict[str, Any], payload: dict[str, Any] | None = None, **env: str) -> None:
    """Republish the fixture app with environment or config overrides."""
    paths = replace(signin["paths"], process_env={**signin["paths"].process_env, **env})
    main.initialize_api_app(main.app, paths)
    _publish_config(main.app, paths, payload or signin["payload"])
    _use_runtime_auth_settings(main.app)


def _connect_state(client: TestClient) -> str:
    """Start the portal's OAuth flow and return its pending state."""
    response = client.post(
        "/api/connections/agents/personal/google_drive/connect",
        headers={"Origin": PORTAL_ORIGIN},
        json={},
    )
    assert response.status_code == 200, response.text
    return parse_qs(urlparse(response.json()["auth_url"]).query)["state"][0]


def test_sign_in_sets_portal_session_cookie(signin: dict[str, Any]) -> None:
    """A verified token yields the Matrix user and one hardened cookie that resolves to that user."""
    response = sign_in(signin["client"], "alice")
    assert response.status_code == 200, response.text
    assert response.json() == {"matrix_user_id": "@alice:example.org"}
    cookie = response.headers["set-cookie"]
    for attribute in (f"{CONNECTIONS_SESSION_COOKIE}=", "Secure", "HttpOnly", "SameSite=lax", "Path=/", "Max-Age=3600"):
        assert attribute in cookie
    assert "Domain" not in cookie
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["referrer-policy"] == "no-referrer"
    token = signin["client"].cookies.get(CONNECTIONS_SESSION_COOKIE)
    assert token is not None
    assert config_lifecycle.app_state(main.app).connections_sessions.resolve(token) == "@alice:example.org"
    assert signin["verifier"].await_args.args[0].access_token == "alice"  # noqa: S105
    assert signin["verifier"].await_args.args[1] == signin["paths"]


def test_sign_in_rejects_client_origin_not_allowlisted(signin: dict[str, Any]) -> None:
    """A page outside the allowlist cannot sign a victim in, and the verifier is never asked."""
    response = sign_in(signin["client"], "alice", origin="https://evil.example.org")
    assert response.status_code == 403
    assert response.json() == {"detail": "Connections sign-in is not allowed from this client"}
    assert "set-cookie" not in response.headers
    signin["verifier"].assert_not_awaited()


def test_sign_in_rejects_cross_origin_request(signin: dict[str, Any]) -> None:
    """Only the portal's own origin may post a sign-in."""
    response = signin["client"].post(
        "/api/connections/session",
        headers={"Origin": "https://evil.example.org"},
        json={
            "openid_token": {
                "access_token": "alice",
                "token_type": "Bearer",
                "matrix_server_name": "example.org",
                "expires_in": 300,
            },
            "client_origin": CHAT_ORIGIN,
        },
    )
    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    signin["verifier"].assert_not_awaited()


def test_sign_in_requires_https_public_origin(signin: dict[str, Any]) -> None:
    """A cleartext public origin never receives a Secure session cookie."""
    _serve(signin, MINDROOM_PUBLIC_URL="http://portal.example.org")
    response = sign_in(TestClient(main.app, base_url="http://portal.example.org"), "alice")
    assert response.status_code == 403
    assert response.json() == {"detail": "Connections require an HTTPS public origin"}
    assert "set-cookie" not in response.headers


def test_sign_in_404_when_portal_disabled(signin: dict[str, Any]) -> None:
    """The portal is opt-in, so a blank agent hides the endpoint."""
    _serve(signin, MINDROOM_CONNECTIONS_AGENT="")
    response = sign_in(signin["client"], "alice")
    assert response.status_code == 404
    assert response.json() == {"detail": "Connections are not enabled"}
    assert "set-cookie" not in response.headers


def test_sign_in_rejects_bad_token_without_echo(signin: dict[str, Any]) -> None:
    """Neither a rejected nor a malformed request may echo the submitted token."""
    secret = "not-a-user-secret"  # noqa: S105
    rejected = sign_in(signin["client"], secret)
    assert rejected.status_code == 401
    assert rejected.json() == {"detail": "Matrix OpenID verification failed."}
    assert secret not in rejected.text
    assert "set-cookie" not in rejected.headers

    malformed = signin["client"].post(
        "/api/connections/session",
        headers={"Origin": PORTAL_ORIGIN},
        json={
            "openid_token": {
                "access_token": secret,
                "token_type": "Bearer",
                "matrix_server_name": "example.org",
                "expires_in": 300,
            },
            "client_origin": CHAT_ORIGIN,
            "unexpected": True,
        },
    )
    assert malformed.status_code == 401
    assert malformed.json() == {"detail": "Invalid sign-in request"}
    assert secret not in malformed.text
    assert "no-store" in malformed.headers["cache-control"]
    assert "set-cookie" not in malformed.headers


def test_sign_in_maps_unavailable_verifier(signin: dict[str, Any]) -> None:
    """A homeserver outage is reported as such, not as bad credentials."""
    signin["verifier"].side_effect = MatrixOpenIDError(503, "Matrix OpenID verifier is unavailable.")
    response = sign_in(signin["client"], "alice")
    assert response.status_code == 503
    assert response.json() == {"detail": "Matrix OpenID verifier is unavailable."}
    assert "set-cookie" not in response.headers


def test_session_reads_catalog_as_matrix_user(signin: dict[str, Any]) -> None:
    """A portal session authenticates the catalog and reports its Matrix user."""
    client = signin["client"]
    assert client.get("/api/connections").status_code == 401
    anonymous = client.get("/api/connections/session")
    assert anonymous.status_code == 401
    assert anonymous.json() == {"detail": "Connections sign-in required"}
    assert "no-store" in anonymous.headers["cache-control"]
    assert anonymous.headers["referrer-policy"] == "no-referrer"

    assert sign_in(client, "alice").status_code == 200
    catalog = client.get("/api/connections")
    assert catalog.status_code == 200, catalog.text
    assert catalog.json()["agents"][0]["agent_display_name"] == "Personal Mind"
    session = client.get("/api/connections/session")
    assert session.status_code == 200
    assert session.json() == {"matrix_user_id": "@alice:example.org"}
    assert "no-store" in session.headers["cache-control"]
    assert session.headers["referrer-policy"] == "no-referrer"


def test_session_endpoint_404_when_portal_disabled(signin: dict[str, Any]) -> None:
    """A disabled portal hides the session probe even from a signed-in browser."""
    assert sign_in(signin["client"], "alice").status_code == 200
    _serve(signin, MINDROOM_CONNECTIONS_AGENT="")
    response = signin["client"].get("/api/connections/session")
    assert response.status_code == 404
    assert response.json() == {"detail": "Connections are not enabled"}


def test_two_session_users_connect_under_their_own_ids_not_owner(signin: dict[str, Any]) -> None:
    """OAuth flows started under a portal session store credentials for that Matrix user, never the owner."""
    clients = {name: TestClient(main.app, base_url=PORTAL_ORIGIN) for name in ("alice", "bob")}
    for name, client in clients.items():
        assert sign_in(client, name).status_code == 200
    alice, bob = clients["alice"], clients["bob"]
    status_url = "/api/connections/agents/personal/google_drive/status"
    callback_url = "/api/oauth/google_drive/callback"
    for client in clients.values():
        assert client.get(status_url).json()["connected"] is False

    state = _connect_state(alice)
    wrong_user = bob.get(callback_url, params={"code": "test-code", "state": state}, follow_redirects=False)
    assert wrong_user.status_code == 403
    assert wrong_user.json() == {"detail": "OAuth state does not belong to the current user"}

    # Bob's attempt did not consume the state, so alice still finishes her own flow with it.
    callback = alice.get(callback_url, params={"code": "test-code", "state": state}, follow_redirects=False)
    assert callback.status_code in {302, 303, 307}, callback.text
    assert alice.get(callback.headers["location"]).status_code == 200
    assert alice.get(callback_url, params={"code": "test-code", "state": state}).status_code == 400

    stored = partial(_stored_oauth_credentials, signin["provider"], signin["paths"], agent_name="personal")
    assert stored(requester_id="@alice:example.org") is not None
    assert stored(requester_id="@bob:example.org") is None
    assert stored(requester_id="@owner:example.org") is None
    assert alice.get(status_url).json()["connected"] is True
    assert bob.get(status_url).json()["connected"] is False

    disconnect_url = "/api/connections/agents/personal/google_drive/disconnect"
    assert bob.post(disconnect_url, headers={"Origin": PORTAL_ORIGIN}, json={}).status_code == 200
    assert alice.get(status_url).json()["connected"] is True
    assert alice.post(disconnect_url, headers={"Origin": PORTAL_ORIGIN}, json={}).status_code == 200
    assert alice.get(status_url).json()["connected"] is False


def test_session_ignored_on_dashboard_routes(signin: dict[str, Any]) -> None:
    """A portal session is not a dashboard credential; the API key still is."""
    client = signin["client"]
    assert sign_in(client, "alice").status_code == 200
    assert client.get("/api/config/agents").status_code == 401
    assert client.get("/api/config/agents", headers={"Authorization": "Bearer dashboard-key"}).status_code == 200


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/credentials/list"),
        ("GET", "/api/oauth/google_drive/status"),
        ("POST", "/api/oauth/google_drive/connect"),
        ("POST", "/api/oauth/google_drive/disconnect"),
    ],
)
def test_session_ignored_on_non_portal_oauth_and_credential_routes(
    method: str,
    path: str,
    signin: dict[str, Any],
) -> None:
    """Only the OAuth popup completion routes accept a session, not the dashboard OAuth controls."""
    client = signin["client"]
    assert sign_in(client, "alice").status_code == 200
    response = client.request(method, path, headers={"Origin": PORTAL_ORIGIN})
    assert response.status_code == 401


def test_live_session_ignored_once_portal_is_disabled(signin: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabling the portal stops honoring live sessions, even on the OAuth completion routes."""
    client = signin["client"]
    assert sign_in(client, "alice").status_code == 200
    assert client.get("/api/oauth/google_drive/success").status_code == 200
    store = config_lifecycle.app_state(main.app).connections_sessions
    _serve(signin, MINDROOM_CONNECTIONS_AGENT="")
    monkeypatch.setattr(config_lifecycle.app_state(main.app), "connections_sessions", store)
    assert client.get("/api/oauth/google_drive/success").status_code == 401


def test_session_cookie_ignored_for_admin_on_dashboard_routes(
    signin: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even an administrator's portal session never opens administrator routes; their signed upstream identity does."""
    key = _trusted_upstream_jwt_key()
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _client: _trusted_upstream_jwks(key))
    env = {name: value for name, value in signin["paths"].process_env.items() if name != "MINDROOM_API_KEY"}
    env.update(_trusted_upstream_strict_jwt_env(tmp_path, matrix_user_id_claim="matrix_user_id"))
    paths = replace(signin["paths"], process_env=env)
    main.initialize_api_app(main.app, paths)
    _publish_config(main.app, paths, {**signin["payload"], "administrators": ["@alice:example.org"]})
    _use_runtime_auth_settings(main.app)
    client = TestClient(main.app, base_url=PORTAL_ORIGIN)

    assert sign_in(client, "alice").status_code == 200
    assert client.get("/api/connections/session").json() == {"matrix_user_id": "@alice:example.org"}
    assert client.get("/api/config/agents").status_code == 401
    upstream = {
        "X-Trusted-User": "alice",
        "X-Trusted-Jwt": _trusted_upstream_jwt(
            key,
            user_id="alice",
            email="alice@example.org",
            matrix_user_id="@alice:example.org",
        ),
    }
    assert client.get("/api/config/agents", headers=upstream).status_code == 200


def test_expired_session_is_rejected(signin: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """A session stops authenticating once its fixed lifetime ends."""
    now = [1000.0]
    store = ConnectionsSessionStore(clock=lambda: now[0])
    monkeypatch.setattr(config_lifecycle.app_state(main.app), "connections_sessions", store)
    client = signin["client"]
    assert sign_in(client, "alice").status_code == 200
    assert client.get("/api/connections").status_code == 200
    now[0] += 3600
    assert client.get("/api/connections").status_code == 401


def test_connections_shell_served_without_auth(
    signin: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The portal page is a public static shell; it signs in itself, so it needs no credential."""
    dist = tmp_path / "dist"
    (dist / "connections").mkdir(parents=True)
    (dist / "index.html").write_text("administrator dashboard")
    (dist / "connections" / "index.html").write_text("<!doctype html><title>Connections</title>")
    monkeypatch.setattr(frontend, "ensure_frontend_dist_dir", lambda _runtime_paths: dist)
    client = signin["client"]

    response = client.get("/connections/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == "<!doctype html><title>Connections</title>"
    assert client.get("/", follow_redirects=False).headers["location"].startswith("/login")

    _serve(signin, MINDROOM_CONNECTIONS_AGENT="")
    assert client.get("/connections/", follow_redirects=False).status_code == 404


@pytest.mark.asyncio
async def test_session_mutations_require_same_origin(signin: dict[str, Any]) -> None:
    """A signed-in browser cannot be driven into portal changes from another origin."""
    client = signin["client"]
    assert sign_in(client, "alice").status_code == 200
    response = client.post(
        "/api/connections/agents/personal/google_drive/connect",
        headers={"Origin": "https://evil.example.org"},
        json={},
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "Connection changes require a same-origin request"}

    # The shared authenticator enforces the same rule for every session-authenticated mutation.
    token = client.cookies.get(CONNECTIONS_SESSION_COOKIE)
    request = Request(
        {
            "type": "http",
            "app": main.app,
            "method": "POST",
            "scheme": "https",
            "server": ("portal.example.org", 443),
            "path": "/api/connections/agents/personal/google_drive/disconnect",
            "query_string": b"",
            "headers": [
                (b"host", b"portal.example.org"),
                (b"cookie", f"{CONNECTIONS_SESSION_COOKIE}={token}".encode()),
                (b"origin", b"https://evil.example.org"),
            ],
        },
    )
    with pytest.raises(HTTPException) as rejected:
        await auth.authenticate_user(request, None)
    assert rejected.value.status_code == 403
    assert rejected.value.detail == "Browser changes require a same-origin request"


def _complete_callback(client: TestClient, state: str) -> None:
    """Finish one OAuth flow in the popup: provider callback, then the success page."""
    callback = client.get(
        "/api/oauth/google_drive/callback",
        params={"code": "test-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code in {302, 303, 307}, callback.text
    assert client.get(callback.headers["location"]).status_code == 200


def test_dashboard_oauth_flow_completes_with_portal_session_present(signin: dict[str, Any]) -> None:
    """A dashboard-started flow finishes as the dashboard requester even when the browser also holds a portal session."""
    client = signin["client"]
    assert sign_in(client, "alice").status_code == 200
    assert client.post("/api/auth/session", json={"api_key": "dashboard-key"}).status_code == 200
    connect = client.post(
        "/api/oauth/google_drive/connect",
        params={"agent_name": "personal"},
        headers={"Origin": PORTAL_ORIGIN},
    )
    assert connect.status_code == 200, connect.text
    _complete_callback(client, parse_qs(urlparse(connect.json()["auth_url"]).query)["state"][0])
    stored = partial(_stored_oauth_credentials, signin["provider"], signin["paths"], agent_name="personal")
    assert stored(requester_id="@owner:example.org") is not None
    assert stored(requester_id="@alice:example.org") is None


def test_portal_oauth_flow_completes_with_dashboard_login_present(signin: dict[str, Any]) -> None:
    """A portal-started flow finishes as the portal user even when the browser also holds a dashboard login."""
    client = signin["client"]
    assert sign_in(client, "alice").status_code == 200
    assert client.post("/api/auth/session", json={"api_key": "dashboard-key"}).status_code == 200
    _complete_callback(client, _connect_state(client))
    stored = partial(_stored_oauth_credentials, signin["provider"], signin["paths"], agent_name="personal")
    assert stored(requester_id="@alice:example.org") is not None
    assert stored(requester_id="@owner:example.org") is None


def _dashboard_client() -> TestClient:
    """Return a browser holding only a dashboard login."""
    client = TestClient(main.app, base_url=PORTAL_ORIGIN)
    assert client.post("/api/auth/session", json={"api_key": "dashboard-key"}).status_code == 200
    return client


def _dashboard_connect_state(client: TestClient) -> str:
    """Start a dashboard OAuth flow and return its pending state."""
    connect = client.post(
        "/api/oauth/google_drive/connect",
        params={"agent_name": "personal"},
        headers={"Origin": PORTAL_ORIGIN},
    )
    assert connect.status_code == 200, connect.text
    return parse_qs(urlparse(connect.json()["auth_url"]).query)["state"][0]


def test_dashboard_flow_callback_rejects_portal_session_only(signin: dict[str, Any]) -> None:
    """A portal session cannot finish a dashboard-started flow, and the refusal leaves the state usable."""
    dashboard = _dashboard_client()
    state = _dashboard_connect_state(dashboard)
    portal = signin["client"]
    assert sign_in(portal, "alice").status_code == 200

    rejected = portal.get(
        "/api/oauth/google_drive/callback",
        params={"code": "test-code", "state": state},
        follow_redirects=False,
    )
    assert rejected.status_code == 401

    _complete_callback(dashboard, state)
    stored = partial(_stored_oauth_credentials, signin["provider"], signin["paths"], agent_name="personal")
    assert stored(requester_id="@owner:example.org") is not None
    assert stored(requester_id="@alice:example.org") is None


def test_portal_flow_callback_rejects_dashboard_login_only(signin: dict[str, Any]) -> None:
    """A dashboard login cannot finish a portal-started flow, and the refusal leaves the state usable."""
    portal = signin["client"]
    assert sign_in(portal, "alice").status_code == 200
    state = _connect_state(portal)

    rejected = _dashboard_client().get(
        "/api/oauth/google_drive/callback",
        params={"code": "test-code", "state": state},
        follow_redirects=False,
    )
    assert rejected.status_code == 403
    assert rejected.json() == {"detail": "OAuth state does not belong to the current user"}

    _complete_callback(portal, state)
    stored = partial(_stored_oauth_credentials, signin["provider"], signin["paths"], agent_name="personal")
    assert stored(requester_id="@alice:example.org") is not None
    assert stored(requester_id="@owner:example.org") is None


@pytest.fixture
def binding_homeserver(signin: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[ComputerPeer]:
    """Verify tokens for real against a fake homeserver that binds OpenID tokens, served from a thread."""
    peer = ComputerPeer(advertise_audience=True)
    upstream = web.Application()
    upstream.router.add_get("/_matrix/client/versions", peer.versions)
    upstream.router.add_get("/_matrix/federation/v1/openid/userinfo", peer.openid)
    runner = web.AppRunner(upstream, access_log=None)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    def run(coroutine: Coroutine[Any, Any, Any]) -> None:
        asyncio.run_coroutine_threadsafe(coroutine, loop).result(timeout=10)

    run(runner.setup())
    run(web.TCPSite(runner, "127.0.0.1", 0).start())
    monkeypatch.setattr(connections_session, "verify_matrix_openid", verify_matrix_openid)
    env = {"MATRIX_HOMESERVER": f"http://127.0.0.1:{runner.addresses[0][1]}", "MATRIX_SERVER_NAME": "example.org"}
    signin["paths"] = replace(signin["paths"], process_env={**signin["paths"].process_env, **env})
    _serve(signin)
    try:
        yield peer
    finally:
        run(runner.cleanup())
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        loop.close()


def test_sign_in_binds_the_token_to_the_configured_origin_not_the_host_header(
    signin: dict[str, Any],
    binding_homeserver: ComputerPeer,
) -> None:
    """A replaying backend controls `Host`, so a binding homeserver still sees MINDROOM_PUBLIC_URL's origin."""
    binding_homeserver.expected_audience = PORTAL_ORIGIN
    response = sign_in(signin["client"], "openid-secret", headers={"Host": "evil.example.org"})
    assert response.status_code == 200, response.text
    assert binding_homeserver.userinfo_audiences == [PORTAL_ORIGIN]


def test_sign_in_with_binding_requires_the_public_url(signin: dict[str, Any], binding_homeserver: ComputerPeer) -> None:
    """Without MINDROOM_PUBLIC_URL the origin of the request is never trusted as the audience."""
    _serve(signin, MINDROOM_PUBLIC_URL="")
    response = sign_in(signin["client"], "openid-secret")
    assert response.status_code == 503
    assert response.json() == {"detail": "Set MINDROOM_PUBLIC_URL to verify bound Matrix OpenID tokens."}
    assert "set-cookie" not in response.headers
    assert binding_homeserver.userinfo_audiences == []
