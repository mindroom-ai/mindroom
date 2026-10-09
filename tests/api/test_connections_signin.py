"""Connections portal sign-in: a Matrix OpenID token becomes a portal session cookie."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from mindroom.api import config_lifecycle, connections_session, main
from mindroom.api.connections_sessions import CONNECTIONS_SESSION_COOKIE
from mindroom.matrix_openid import MatrixOpenIDError
from tests.api.test_oauth_api import _publish_config, _runtime_paths, _use_runtime_auth_settings

if TYPE_CHECKING:
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
    env = {
        "MINDROOM_API_KEY": "dashboard-key",
        "MINDROOM_OWNER_USER_ID": "@owner:example.org",
        "MINDROOM_CONNECTIONS_AGENT": "personal",
        "MINDROOM_CONNECTIONS_ALLOWED_ORIGINS": f'["{CHAT_ORIGIN}"]',
        "MINDROOM_PUBLIC_URL": PORTAL_ORIGIN,
    }
    paths = _runtime_paths(tmp_path, env)
    payload = {
        "models": {"default": {"provider": "ollama", "id": "test-model"}},
        "agents": {
            "personal": {
                "display_name": "Personal Mind",
                "role": "Personal assistant",
                "tools": ["calculator"],
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
        "verifier": verifier,
    }


def sign_in(client: TestClient, name: str, origin: str = CHAT_ORIGIN) -> Response:
    """Post a Matrix OpenID token for `name` the way the portal page does."""
    return client.post(
        "/api/connections/session",
        headers={"Origin": PORTAL_ORIGIN},
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
    paths = replace(
        signin["paths"],
        process_env={**signin["paths"].process_env, "MINDROOM_PUBLIC_URL": "http://portal.example.org"},
    )
    main.initialize_api_app(main.app, paths)
    _publish_config(main.app, paths, signin["payload"])
    _use_runtime_auth_settings(main.app)
    response = sign_in(TestClient(main.app, base_url="http://portal.example.org"), "alice")
    assert response.status_code == 403
    assert response.json() == {"detail": "Connections require an HTTPS public origin"}
    assert "set-cookie" not in response.headers


def test_sign_in_404_when_portal_disabled(signin: dict[str, Any]) -> None:
    """The portal is opt-in, so a blank agent hides the endpoint."""
    paths = replace(signin["paths"], process_env={**signin["paths"].process_env, "MINDROOM_CONNECTIONS_AGENT": ""})
    main.initialize_api_app(main.app, paths)
    _publish_config(main.app, paths, signin["payload"])
    _use_runtime_auth_settings(main.app)
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
