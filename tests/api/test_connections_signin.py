"""Connections portal sign-in: a Matrix OpenID token becomes a portal session cookie."""

from __future__ import annotations

import asyncio
import threading
import time
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
from structlog.testing import capture_logs

from mindroom.api import auth, connections_session, frontend, main
from mindroom.api.connections_session_cookie import (
    connections_session_cookie,
    issue_connections_session,
    read_connections_session,
)
from mindroom.matrix_openid import MatrixOpenIDError, homeserver_binds_openid_audience, verify_matrix_openid
from mindroom.oauth import registry as oauth_registry
from tests.api.test_api import (
    _trusted_upstream_jwks,
    _trusted_upstream_jwt,
    _trusted_upstream_jwt_key,
    _trusted_upstream_strict_jwt_env,
)
from tests.api.test_oauth_api import (
    _expect_styled_oauth_page,
    _fake_provider,
    _publish_config,
    _runtime_paths,
    _stored_oauth_credentials,
    _use_runtime_auth_settings,
)
from tests.computer_helpers import ComputerPeer

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterator
    from pathlib import Path

    from httpx import Response

PORTAL_ORIGIN = "https://portal.example.org"
CHAT_ORIGIN = "https://chat.example.org"
CONNECTIONS_SESSION_COOKIE = connections_session_cookie(PORTAL_ORIGIN)


@pytest.fixture
def signin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enforce_turn_authorization: None) -> dict[str, Any]:  # noqa: ARG001
    """Serve the real API in API-key mode with a fake Matrix OpenID verifier for users alice and bob."""

    async def verify(token: Any, _paths: Any) -> str:  # noqa: ANN401
        if token.access_token in {"alice", "bob", "mallory"}:
            return f"@{token.access_token}:example.org"
        raise MatrixOpenIDError(401, "Matrix OpenID verification failed.")

    verifier = AsyncMock(side_effect=verify)
    monkeypatch.setattr(connections_session, "verify_matrix_openid", verifier)
    binds = AsyncMock(return_value=False)
    monkeypatch.setattr(connections_session, "homeserver_binds_openid_audience", binds)
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
        "binds": binds,
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
    assert read_connections_session(token, now=time.time()) == ("@alice:example.org", False)
    assert signin["verifier"].await_args.args[0].access_token == "alice"  # noqa: S105
    assert signin["verifier"].await_args.args[1] == signin["paths"]


def _session_of(client: TestClient) -> tuple[str, bool] | None:
    """Read the portal session cookie the client holds."""
    token = client.cookies.get(CONNECTIONS_SESSION_COOKIE)
    return read_connections_session(token, now=time.time()) if token else None


def _with_admin(signin: dict[str, Any], admin: str = "@alice:example.org", **env: str) -> None:
    _serve(signin, {**signin["payload"], "administrators": [admin]}, **env)


def test_admin_sign_in_on_binding_homeserver_gets_dashboard_bit(signin: dict[str, Any]) -> None:
    """An administrator whose token the homeserver bound to this deployment may open the dashboard."""
    _with_admin(signin)
    signin["binds"].return_value = True
    assert sign_in(signin["client"], "alice").status_code == 200
    assert _session_of(signin["client"]) == ("@alice:example.org", True)


@pytest.mark.parametrize(
    ("admin", "binds"),
    [("@bob:example.org", True), ("@alice:example.org", False)],
    ids=["not-an-administrator", "homeserver-does-not-bind"],
)
def test_dashboard_bit_needs_admin_and_binding(signin: dict[str, Any], admin: str, binds: bool) -> None:
    """Without both administrator authority and token binding, the session stays portal-only."""
    _with_admin(signin, admin)
    signin["binds"].return_value = binds
    assert sign_in(signin["client"], "alice").status_code == 200
    assert _session_of(signin["client"]) == ("@alice:example.org", False)


def test_failed_check_after_binding_probe_logs_why_dashboard_was_refused(signin: dict[str, Any]) -> None:
    """A token check that fails after the binding probe leaves a warning for the administrator to find."""
    _with_admin(signin)
    signin["binds"].return_value = True
    signin["verifier"].side_effect = [
        "@alice:example.org",
        MatrixOpenIDError(503, "Matrix OpenID verifier is unavailable."),
    ]
    with capture_logs() as logs:
        assert sign_in(signin["client"], "alice").status_code == 200
    assert _session_of(signin["client"]) == ("@alice:example.org", False)
    assert [
        log.get("detail") for log in logs if log["event"] == "Dashboard access check failed after the binding probe"
    ] == [
        "Matrix OpenID verifier is unavailable.",
    ]


def test_binding_probe_runs_only_for_administrators(signin: dict[str, Any]) -> None:
    """Signing in an ordinary user never contacts the homeserver's versions endpoint."""
    assert sign_in(signin["client"], "alice").status_code == 200
    assert signin["binds"].await_count == 0


def test_sign_in_refuses_user_without_visible_agents(signin: dict[str, Any]) -> None:
    """Any homeserver user has a valid token, so one without portal access gets no session."""
    response = sign_in(signin["client"], "mallory")
    assert response.status_code == 403, response.text
    assert "set-cookie" not in response.headers


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


@pytest.mark.parametrize("public_url", ["http://portal.example.org", "http://100.64.0.1:8765"])
def test_sign_in_refuses_plain_http_outside_local_addresses(signin: dict[str, Any], public_url: str) -> None:
    """A cleartext public origin gets no session at all."""
    _serve(signin, MINDROOM_PUBLIC_URL=public_url)
    response = sign_in(TestClient(main.app, base_url=public_url), "alice", headers={"Origin": public_url})
    assert response.status_code == 403
    assert response.json() == {"detail": "Connections require an HTTPS public origin, or HTTP on a local address"}
    assert "set-cookie" not in response.headers
    signin["verifier"].assert_not_awaited()


LAN_PORTAL = "http://192.168.1.10:8765"
LAN_CHAT = "http://192.168.1.10:8080"


def test_portal_on_a_local_address_works_over_http_with_no_settings(signin: dict[str, Any]) -> None:
    """A Docker stack on a home network signs Chat users in over HTTP with a cookie the browser keeps there."""
    _serve(signin, MINDROOM_PUBLIC_URL="", MINDROOM_CONNECTIONS_AGENT="", MINDROOM_CONNECTIONS_ALLOWED_ORIGINS="")
    signin["binds"].return_value = True
    client = TestClient(main.app, base_url=LAN_PORTAL)
    response = sign_in(client, "alice", origin=LAN_CHAT, headers={"Origin": LAN_PORTAL})
    assert response.status_code == 200, response.text
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("mindroom_connections_session=")
    assert "Secure" not in cookie
    for attribute in ("HttpOnly", "SameSite=lax", "Path=/", "Max-Age=3600"):
        assert attribute in cookie
    assert client.get("/api/connections/session").json() == {"matrix_user_id": "@alice:example.org"}
    assert client.get("/api/connections").json()["agents"][0]["agent_name"] == "personal"
    connect = client.post(
        "/api/connections/agents/personal/google_drive/connect",
        headers={"Origin": LAN_PORTAL},
        json={},
    )
    assert connect.status_code == 200, connect.text
    # The same session cookie under its HTTPS name means nothing on plain HTTP.
    secure_named = TestClient(main.app, base_url=LAN_PORTAL)
    secure_named.cookies.set(CONNECTIONS_SESSION_COOKIE, client.cookies["mindroom_connections_session"])
    assert secure_named.get("/api/connections").status_code == 401


def test_https_portal_ignores_a_plain_http_session_cookie(signin: dict[str, Any]) -> None:
    """On HTTPS only the `__Host-` cookie counts, so a sibling host cannot plant a session cookie for this one."""
    client = signin["client"]
    client.cookies.set(
        "mindroom_connections_session",
        issue_connections_session("@alice:example.org", now=time.time(), dashboard=False),
    )
    assert client.get("/api/connections").status_code == 401


def test_local_chat_cannot_sign_in_to_a_public_portal_by_default(signin: dict[str, Any]) -> None:
    """Without a list, only MindRoom's hosted Chat signs users in to a portal that is not on a local address."""
    _serve(signin, MINDROOM_CONNECTIONS_AGENT="", MINDROOM_CONNECTIONS_ALLOWED_ORIGINS="")
    signin["binds"].return_value = True
    refused = sign_in(signin["client"], "alice", origin=LAN_CHAT)
    assert refused.status_code == 403
    assert refused.json() == {"detail": "Connections sign-in is not allowed from this client"}
    assert sign_in(signin["client"], "alice", origin="https://chat.mindroom.chat").status_code == 200


@pytest.mark.parametrize("portal", [PORTAL_ORIGIN, LAN_PORTAL, "http://localhost:8765"])
def test_default_chat_origins_wait_for_a_binding_homeserver(signin: dict[str, Any], portal: str) -> None:
    """Unbound tokens could be replayed by any backend the user opened, and a forged `Host` can claim any address.

    So under the default list every portal, local-looking or not, needs binding; an explicit list accepts the risk.
    """
    _serve(signin, MINDROOM_PUBLIC_URL="", MINDROOM_CONNECTIONS_ALLOWED_ORIGINS="")
    client = TestClient(main.app, base_url=portal)
    refused = sign_in(client, "alice", origin="https://chat.mindroom.chat", headers={"Origin": portal})
    assert refused.status_code == 403
    assert refused.json() == {
        "detail": "Connections sign-in from MindRoom Chat needs a homeserver that binds tokens, "
        "or MINDROOM_CONNECTIONS_ALLOWED_ORIGINS",
    }
    assert "set-cookie" not in refused.headers
    signin["verifier"].assert_not_awaited()
    _serve(signin, MINDROOM_PUBLIC_URL="", MINDROOM_CONNECTIONS_ALLOWED_ORIGINS='["https://chat.mindroom.chat"]')
    assert sign_in(client, "alice", origin="https://chat.mindroom.chat", headers={"Origin": portal}).status_code == 200


def test_empty_chat_allowlist_turns_matrix_sign_in_off(signin: dict[str, Any]) -> None:
    """`[]` refuses every Chat, including the defaults for MindRoom's hosted Chat and local addresses."""
    _serve(signin, MINDROOM_PUBLIC_URL="", MINDROOM_CONNECTIONS_ALLOWED_ORIGINS="[]")
    client = TestClient(main.app, base_url=LAN_PORTAL)
    for origin in (LAN_CHAT, "https://chat.mindroom.chat"):
        response = sign_in(client, "alice", origin=origin, headers={"Origin": LAN_PORTAL})
        assert response.status_code == 403
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


def test_portal_needs_no_connections_agent(signin: dict[str, Any]) -> None:
    """Without `MINDROOM_CONNECTIONS_AGENT` the portal still signs users in and lists the private agents they use."""
    _serve(signin, MINDROOM_CONNECTIONS_AGENT="")
    client = signin["client"]
    assert sign_in(client, "alice").status_code == 200
    assert client.get("/api/connections/session").json() == {"matrix_user_id": "@alice:example.org"}
    assert [agent["agent_name"] for agent in client.get("/api/connections").json()["agents"]] == ["personal"]
    assert client.get("/api/oauth/google_drive/success").status_code == 200
    _connect_state(client)


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
    _expect_styled_oauth_page(wrong_user, status=403, message="OAuth state does not belong to the current user")

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


def test_session_refuses_requests_from_a_tab_signed_in_as_another_account(signin: dict[str, Any]) -> None:
    """The cookie is browser-wide, so a stale tab naming another account is refused and acts on nothing."""
    client = signin["client"]
    assert sign_in(client, "alice").status_code == 200
    status_url = "/api/connections/agents/personal/google_drive/status"
    disconnect_url = "/api/connections/agents/personal/google_drive/disconnect"
    state = _connect_state(client)
    callback = client.get(
        "/api/oauth/google_drive/callback",
        params={"code": "test-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code in {302, 303, 307}, callback.text
    stored = partial(_stored_oauth_credentials, signin["provider"], signin["paths"], agent_name="personal")
    assert stored(requester_id="@alice:example.org") is not None

    stale = {"X-MindRoom-Connections-User": "@bob:example.org"}
    for response in (
        client.get("/api/connections", headers=stale),
        client.get(status_url, headers=stale),
        client.post(disconnect_url, headers={"Origin": PORTAL_ORIGIN, **stale}, json={}),
    ):
        assert response.status_code == 401
        assert response.json() == {"detail": "This page shows another account. Reload this page."}
    assert stored(requester_id="@alice:example.org") is not None

    matching = {"X-MindRoom-Connections-User": "@alice:example.org"}
    assert client.get(status_url, headers=matching).json()["connected"] is True
    assert client.get(status_url).json()["connected"] is True
    assert client.post(disconnect_url, headers={"Origin": PORTAL_ORIGIN, **matching}, json={}).status_code == 200
    assert stored(requester_id="@alice:example.org") is None


def test_session_reaches_the_agent_keys_api(signin: dict[str, Any]) -> None:
    """With the broker on, a portal session reads the Agent Keys of its own agents."""
    services = {"github": {"rules": [{"host": "api.github.com", "auth": {"type": "bearer"}}]}}
    # The broker runs exactly when its port is set, and a dedicated backend gives each requester its own worker.
    _serve(
        signin,
        {
            **signin["payload"],
            "agents": {"personal": {**signin["payload"]["agents"]["personal"], "tools": ["shell"]}},
            "egress_broker": {"services": services},
        },
        MINDROOM_EGRESS_BROKER_PORT="8768",
        MINDROOM_WORKER_BACKEND="docker",
    )
    client = signin["client"]
    assert client.get("/api/connections/egress").status_code == 401
    assert sign_in(client, "alice").status_code == 200
    response = client.get("/api/connections/egress")
    assert response.status_code == 200, response.text
    (agent,) = response.json()["agents"]
    assert agent["agent_name"] == "personal"
    assert agent["can_manage"] is True


def test_session_ignored_on_dashboard_routes(signin: dict[str, Any]) -> None:
    """A portal session is not a dashboard credential; the API key still is."""
    client = signin["client"]
    assert sign_in(client, "alice").status_code == 200
    assert client.get("/api/config/agents").status_code == 401
    assert client.get("/api/config/agents", headers={"Authorization": "Bearer dashboard-key"}).status_code == 200


def _admin_dashboard_client(signin: dict[str, Any]) -> TestClient:
    """Return a browser signed in from Chat as administrator alice on a homeserver that binds tokens."""
    _with_admin(signin)
    signin["binds"].return_value = True
    client = signin["client"]
    assert sign_in(client, "alice").status_code == 200
    return client


def test_admin_dashboard_session_reaches_dashboard_api(signin: dict[str, Any]) -> None:
    """An administrator signed in with a bound token uses the dashboard API without the API key."""
    client = _admin_dashboard_client(signin)
    assert client.get("/api/config/agents").status_code == 200


def test_admin_dashboard_session_loads_dashboard_page(
    signin: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dashboard shell loads instead of redirecting the administrator to the API-key login."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("administrator dashboard")
    monkeypatch.setattr(frontend, "ensure_frontend_dist_dir", lambda _runtime_paths: dist)
    client = _admin_dashboard_client(signin)

    response = client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 200
    assert response.text == "administrator dashboard"


def test_session_without_dashboard_bit_keeps_api_key_rule(signin: dict[str, Any]) -> None:
    """An administrator whose sign-in was not proven bound still needs the API key for the dashboard."""
    _with_admin(signin)
    client = signin["client"]
    assert sign_in(client, "alice").status_code == 200
    assert client.get("/api/config/agents").status_code == 401
    assert client.get("/api/config/agents", headers={"Authorization": "Bearer dashboard-key"}).status_code == 200


def test_removed_admin_loses_dashboard_access_next_request(signin: dict[str, Any]) -> None:
    """Administrator authority is read on every request, not frozen into the session."""
    client = _admin_dashboard_client(signin)
    _serve(signin)
    assert client.get("/api/config/agents").status_code == 401


def test_dashboard_session_ignored_in_platform_mode(signin: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosted platform dashboards keep their account ownership check, so the session never authenticates them."""
    _admin_dashboard_client(signin)
    token = signin["client"].cookies.get(CONNECTIONS_SESSION_COOKIE)
    monkeypatch.setattr(auth, "_init_supabase_auth", lambda _paths, _settings: object())
    _with_admin(signin)

    def session_user(path: str) -> dict[str, Any] | None:
        request = Request(
            {
                "type": "http",
                "app": main.app,
                "method": "GET",
                "scheme": "https",
                "server": ("portal.example.org", 443),
                "path": path,
                "query_string": b"",
                "headers": [
                    (b"host", b"portal.example.org"),
                    (b"cookie", f"{CONNECTIONS_SESSION_COOKIE}={token}".encode()),
                ],
            },
        )
        return auth._connections_session_auth_user(request)

    assert session_user("/api/config/agents") is None
    assert session_user("/api/connections/session") is not None


def test_dashboard_session_cannot_finish_api_key_started_flow(signin: dict[str, Any]) -> None:
    """A flow started with the API key cannot be finished by an administrator's dashboard session."""
    dashboard = _dashboard_client()
    state = _dashboard_connect_state(dashboard)
    portal = _admin_dashboard_client(signin)

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


def test_dashboard_session_mutation_requires_same_origin(signin: dict[str, Any]) -> None:
    """Dashboard changes made with the session keep the same-origin rule."""
    client = _admin_dashboard_client(signin)
    connect = partial(client.post, "/api/oauth/google_drive/connect", params={"agent_name": "personal"})
    assert connect(headers={"Origin": "https://evil.example"}).status_code == 403
    assert connect(headers={"Origin": PORTAL_ORIGIN}).status_code == 200


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


def _serve_with_strict_upstream(
    signin: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> tuple[TestClient, Callable[[str], dict[str, str]]]:
    """Serve the portal with strict trusted-upstream JWT next to sessions; return a client and upstream headers per user."""
    key = _trusted_upstream_jwt_key()
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _client: _trusted_upstream_jwks(key))
    env = {name: value for name, value in signin["paths"].process_env.items() if name != "MINDROOM_API_KEY"}
    env.update(_trusted_upstream_strict_jwt_env(tmp_path, matrix_user_id_claim="matrix_user_id"))
    paths = replace(signin["paths"], process_env=env)
    main.initialize_api_app(main.app, paths)
    _publish_config(main.app, paths, payload)
    _use_runtime_auth_settings(main.app)

    def upstream(name: str) -> dict[str, str]:
        return {
            "X-Trusted-User": name,
            "X-Trusted-Jwt": _trusted_upstream_jwt(
                key,
                user_id=name,
                email=f"{name}@example.org",
                matrix_user_id=f"@{name}:example.org",
            ),
        }

    return TestClient(main.app, base_url=PORTAL_ORIGIN), upstream


def test_session_cookie_ignored_for_admin_on_dashboard_routes(
    signin: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without token binding, an administrator's portal session never opens administrator routes."""
    payload = {**signin["payload"], "administrators": ["@alice:example.org"]}
    client, upstream = _serve_with_strict_upstream(signin, tmp_path, monkeypatch, payload)

    assert sign_in(client, "alice").status_code == 200
    assert client.get("/api/connections/session").json() == {"matrix_user_id": "@alice:example.org"}
    assert client.get("/api/config/agents").status_code == 401
    assert client.get("/api/config/agents", headers=upstream("alice")).status_code == 200


def test_upstream_identity_refuses_a_page_showing_another_account(
    signin: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once a page's session is gone, the browser's upstream identity cannot act for the account the page shows."""
    client, upstream = _serve_with_strict_upstream(signin, tmp_path, monkeypatch, signin["payload"])

    stale = client.get(
        "/api/connections/session",
        headers={**upstream("bob"), "X-MindRoom-Connections-User": "@alice:example.org"},
    )
    assert stale.status_code == 401
    assert stale.json() == {"detail": "This page shows another account. Reload this page."}
    matching = client.get(
        "/api/connections/session",
        headers={**upstream("bob"), "X-MindRoom-Connections-User": "@bob:example.org"},
    )
    assert matching.json() == {"matrix_user_id": "@bob:example.org"}


def test_expired_session_is_rejected(signin: dict[str, Any]) -> None:
    """A session stops authenticating once its fixed lifetime ends."""
    client = signin["client"]
    for issued_at, status in ((time.time(), 200), (time.time() - 3600, 401)):
        token = issue_connections_session("@alice:example.org", now=issued_at, dashboard=False)
        client.cookies.set(CONNECTIONS_SESSION_COOKIE, token)
        assert client.get("/api/connections").status_code == status


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
    _expect_styled_oauth_page(rejected, status=403, message="OAuth state does not belong to the current user")

    _complete_callback(portal, state)
    stored = partial(_stored_oauth_credentials, signin["provider"], signin["paths"], agent_name="personal")
    assert stored(requester_id="@alice:example.org") is not None
    assert stored(requester_id="@owner:example.org") is None


@pytest.fixture
def binding_homeserver(signin: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[ComputerPeer]:
    """Verify tokens for real against a fake homeserver that can bind OpenID tokens, served from a thread."""
    peer = ComputerPeer()
    upstream = web.Application()
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


def test_sign_in_never_takes_the_audience_from_the_request(
    signin: dict[str, Any],
    binding_homeserver: ComputerPeer,
) -> None:
    """Without MINDROOM_PUBLIC_URL the audience is the local default, so a token bound to the request's origin is refused."""
    binding_homeserver.expected_audience = PORTAL_ORIGIN
    _serve(signin, MINDROOM_PUBLIC_URL="")
    response = sign_in(signin["client"], "openid-secret")
    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert binding_homeserver.userinfo_audiences == ["http://localhost:8765"]


def test_binding_switched_on_during_sign_in_grants_no_dashboard(
    signin: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unbound token verified just before the homeserver starts binding never becomes a dashboard session.

    The fake homeserver accepts the unbound token while binding is off and switches binding on as `/versions` is read,
    which is the replay window a separate capability check alone would leave open.
    """
    binding = {"on": False}

    async def userinfo(request: web.Request) -> web.Response:
        if binding["on"] and request.query.get("io.mindroom.audience") is not None:
            return web.json_response({"errcode": "M_UNKNOWN_TOKEN"}, status=401)
        return web.json_response({"sub": "@alice:example.org"})

    async def versions(_request: web.Request) -> web.Response:
        binding["on"] = True
        return web.json_response({"versions": ["v1.11"], "unstable_features": {"io.mindroom.openid_audience": True}})

    upstream = web.Application()
    upstream.router.add_get("/_matrix/federation/v1/openid/userinfo", userinfo)
    upstream.router.add_get("/_matrix/client/versions", versions)
    runner = web.AppRunner(upstream, access_log=None)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    def run(coroutine: Coroutine[Any, Any, Any]) -> None:
        asyncio.run_coroutine_threadsafe(coroutine, loop).result(timeout=10)

    run(runner.setup())
    run(web.TCPSite(runner, "127.0.0.1", 0).start())
    try:
        monkeypatch.setattr(connections_session, "verify_matrix_openid", verify_matrix_openid)
        monkeypatch.setattr(connections_session, "homeserver_binds_openid_audience", homeserver_binds_openid_audience)
        homeserver = f"http://127.0.0.1:{runner.addresses[0][1]}"
        _with_admin(signin, MATRIX_HOMESERVER=homeserver, MATRIX_SERVER_NAME="example.org")

        assert sign_in(signin["client"], "alice").status_code == 200
        assert binding["on"] is True
        assert _session_of(signin["client"]) == ("@alice:example.org", False)
        assert signin["client"].get("/api/config/agents").status_code == 401
    finally:
        run(runner.cleanup())
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        loop.close()
