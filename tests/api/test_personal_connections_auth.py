"""Signed identity and static boundaries for the personal connections portal."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import jwt
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from mindroom import constants
from mindroom.api import auth, config_lifecycle, frontend, main
from tests.api.test_api import _trusted_upstream_jwks, _trusted_upstream_jwt, _trusted_upstream_jwt_key


@pytest.fixture
def signed_connections_headers(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], dict[str, str]]:
    """Sign real upstream JWTs while keeping JWKS retrieval local."""
    private_key = _trusted_upstream_jwt_key()
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _client: _trusted_upstream_jwks(private_key))

    def headers(user: str = "alice") -> dict[str, str]:
        email = f"{user}@example.org"
        return {
            "X-Trusted-User": user,
            "X-Trusted-Email": email,
            "X-Trusted-Jwt": _trusted_upstream_jwt(
                private_key,
                user_id=user,
                email=email,
                matrix_user_id=f"@{user}:example.org",
                issuer="https://issuer.example.org",
            ),
        }

    return headers


@pytest.fixture
def connections_auth_client(
    temp_config_file: Path,
    tmp_path: Path,
) -> Callable[..., TestClient]:
    """Build isolated apps exercising production authentication and static routes."""

    def client(**env_overrides: str | None) -> TestClient:
        env = {
            "MINDROOM_CONNECTIONS_AGENT": "test_agent",
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
        for name, value in env_overrides.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        runtime_paths = constants.resolve_primary_runtime_paths(
            config_path=temp_config_file,
            storage_path=tmp_path / "storage",
            process_env=env,
        )
        api_app = FastAPI()
        main.initialize_api_app(api_app, runtime_paths)
        config_lifecycle.load_config_into_app(runtime_paths, api_app)

        @api_app.get("/api/connections/identity")
        async def personal_identity(request: Request) -> dict[str, Any]:
            return await auth.require_personal_connections_user(request)

        @api_app.api_route("/api/{path:path}", methods=["GET", "POST"])
        async def protected_route(user: Annotated[dict[str, Any], Depends(auth.verify_user)]) -> dict[str, Any]:
            return user

        api_app.include_router(frontend.router)
        return TestClient(api_app)

    return client


@pytest.mark.parametrize(
    "path",
    [
        "/api/config",
        "/api/credentials",
        "/api/workers",
        "/api/connections-admin",
        "/api/connections_extra/status",
        "/api/oauth/google_drive/connect",
        "/api/oauth/google_drive/status",
        "/api/oauth/google_drive/disconnect",
        "/api/oauth/google_drive/authorize",
        "/api/oauth/google_drive/callback/extra",
        "/api/oauth/google_drive/success-extra",
    ],
)
def test_signed_nonadmin_cannot_enter_administrator_api(
    path: str,
    connections_auth_client: Callable[..., TestClient],
    signed_connections_headers: Callable[[str], dict[str, str]],
) -> None:
    """Enabling personal access must not grant upstream users administrator API access."""
    response = connections_auth_client().get(path, headers=signed_connections_headers("alice"))
    assert response.status_code == 403


@pytest.mark.parametrize("path", ["/", "/agents", "/connections-admin", "/connections_extra"])
def test_signed_nonadmin_cannot_load_administrator_html(
    path: str,
    connections_auth_client: Callable[..., TestClient],
    signed_connections_headers: Callable[[str], dict[str, str]],
) -> None:
    """Administrator HTML must reject signed personal users before asset resolution."""
    response = connections_auth_client().get(path, headers=signed_connections_headers("alice"))
    assert response.status_code == 403


@pytest.mark.parametrize("user", ["alice", "owner"])
def test_personal_identity_accepts_signed_matrix_user(
    user: str,
    connections_auth_client: Callable[..., TestClient],
    signed_connections_headers: Callable[[str], dict[str, str]],
) -> None:
    """Personal requests keep their own verified identity regardless of admin role."""
    response = connections_auth_client().get("/api/connections/identity", headers=signed_connections_headers(user))
    assert response.status_code == 200
    assert response.json()["matrix_user_id"] == f"@{user}:example.org"


@pytest.mark.parametrize(
    "overrides",
    [
        {"MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "false", "MINDROOM_API_KEY": "owner-key"},
        {"MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT": "false"},
        {"MINDROOM_TRUSTED_UPSTREAM_JWT_MATRIX_USER_ID_CLAIM": None},
    ],
)
def test_personal_identity_rejects_unverified_identity_modes(
    overrides: dict[str, str | None],
    connections_auth_client: Callable[..., TestClient],
    signed_connections_headers: Callable[[str], dict[str, str]],
) -> None:
    """API keys, unsigned headers and JWTs without a bound Matrix identity cannot own personal credentials."""
    headers = {**signed_connections_headers("alice"), "Authorization": "Bearer owner-key"}
    response = connections_auth_client(**overrides).get("/api/connections/identity", headers=headers)
    assert response.status_code == 403


def test_personal_identity_rejects_spoofed_headers_without_signed_assertion(
    connections_auth_client: Callable[..., TestClient],
) -> None:
    """Upstream identity headers alone never authenticate the personal endpoint."""
    response = connections_auth_client().get(
        "/api/connections/identity",
        headers={"X-Trusted-User": "owner", "X-Trusted-Email": "owner@example.org"},
    )
    assert response.status_code == 401


@pytest.mark.parametrize(("portal_enabled", "user"), [(True, "owner"), (False, "alice")])
def test_administrator_and_disabled_portal_preserve_dashboard_access(
    portal_enabled: bool,
    user: str,
    connections_auth_client: Callable[..., TestClient],
    signed_connections_headers: Callable[[str], dict[str, str]],
) -> None:
    """Configured administrators retain access, and disabled portals preserve prior upstream behavior."""
    client = connections_auth_client(MINDROOM_CONNECTIONS_AGENT="test_agent" if portal_enabled else None)
    response = client.get("/api/config", headers=signed_connections_headers(user))
    assert response.status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/connections",
        "/api/connections/",
        "/api/connections/google_drive/status",
        "/api/oauth/google_drive/callback",
        "/api/oauth/google_drive/success",
        "/api/oauth/google_drive/reset",
    ],
)
def test_signed_nonadmin_can_enter_personal_and_oauth_completion_routes(
    path: str,
    connections_auth_client: Callable[..., TestClient],
    signed_connections_headers: Callable[[str], dict[str, str]],
) -> None:
    """Authentication leaves state-bound completion validation to existing OAuth handlers."""
    response = connections_auth_client().get(path, headers=signed_connections_headers("alice"))
    assert response.status_code == 200


@pytest.mark.parametrize("portal_setting", [None, "", "   "])
@pytest.mark.parametrize("path", ["/connections", "/connections/assets/portal.js"])
def test_disabled_connections_frontend_is_unavailable(
    portal_setting: str | None,
    path: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    connections_auth_client: Callable[..., TestClient],
    signed_connections_headers: Callable[[str], dict[str, str]],
) -> None:
    """A disabled portal does not serve a frontend whose API cannot be used."""
    portal = tmp_path / "connections"
    (portal / "assets").mkdir(parents=True)
    (portal / "index.html").write_text("personal connections")
    (portal / "assets" / "portal.js").write_text("portal asset")
    monkeypatch.setattr(frontend, "ensure_frontend_dist_dir", lambda _runtime_paths: tmp_path)
    client = connections_auth_client(MINDROOM_CONNECTIONS_AGENT=portal_setting)
    response = client.get(path, headers=signed_connections_headers("alice"))
    assert response.status_code == 404


@pytest.mark.parametrize("path", ["/connections", "/connections/", "/connections/nested"])
def test_missing_connections_bundle_never_returns_administrator_html(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    connections_auth_client: Callable[..., TestClient],
    signed_connections_headers: Callable[[str], dict[str, str]],
) -> None:
    """Missing dedicated assets must fail closed instead of falling back to dashboard HTML."""
    (tmp_path / "index.html").write_text("administrator dashboard")
    monkeypatch.setattr(frontend, "ensure_frontend_dist_dir", lambda _runtime_paths: tmp_path)
    response = connections_auth_client().get(path, headers=signed_connections_headers("alice"))
    assert response.status_code == 404
    assert "administrator dashboard" not in response.text


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("/connections", "personal connections"),
        ("/connections/", "personal connections"),
        ("/connections/nested", "personal connections"),
        ("/connections/assets/portal.js", "portal asset"),
    ],
)
def test_connections_static_routes_use_only_dedicated_bundle(
    path: str,
    content: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    connections_auth_client: Callable[..., TestClient],
    signed_connections_headers: Callable[[str], dict[str, str]],
) -> None:
    """Dedicated pages and assets remain available to authenticated personal users."""
    (tmp_path / "index.html").write_text("administrator dashboard")
    portal = tmp_path / "connections"
    (portal / "assets").mkdir(parents=True)
    (portal / "index.html").write_text("personal connections")
    (portal / "assets" / "portal.js").write_text("portal asset")
    monkeypatch.setattr(frontend, "ensure_frontend_dist_dir", lambda _runtime_paths: tmp_path)
    response = connections_auth_client().get(path, headers=signed_connections_headers("alice"))
    assert response.status_code == 200
    assert response.text == content


@pytest.mark.parametrize(
    "path",
    ["/connections/%2e%2e/index.html", "/connections/%252e%252e/index.html", "/connections/missing.js"],
)
def test_connections_static_rejects_traversal_and_missing_assets(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    connections_auth_client: Callable[..., TestClient],
    signed_connections_headers: Callable[[str], dict[str, str]],
) -> None:
    """Encoded traversal and missing scripts must not expose administrator assets."""
    (tmp_path / "index.html").write_text("administrator dashboard")
    (tmp_path / "connections").mkdir()
    (tmp_path / "connections" / "index.html").write_text("personal connections")
    monkeypatch.setattr(frontend, "ensure_frontend_dist_dir", lambda _runtime_paths: tmp_path)
    response = connections_auth_client().get(path, headers=signed_connections_headers("alice"))
    assert response.status_code == 404
    assert "administrator dashboard" not in response.text


def test_connections_static_rejects_api_key_fallback(
    connections_auth_client: Callable[..., TestClient],
) -> None:
    """Standalone owner credentials cannot authenticate the enabled personal frontend."""
    client = connections_auth_client(MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED="false", MINDROOM_API_KEY="owner-key")
    response = client.get("/connections", headers={"Authorization": "Bearer owner-key"})
    assert response.status_code == 403


def test_administrator_access_uses_canonical_matrix_alias_policy(
    connections_auth_client: Callable[..., TestClient],
    signed_connections_headers: Callable[[str], dict[str, str]],
) -> None:
    """A signed human alias retains its canonical administrator authority."""
    client = connections_auth_client()
    snapshot = config_lifecycle.require_api_state(client.app).snapshot
    assert snapshot.runtime_config is not None
    snapshot.runtime_config.authorization.aliases = {"@owner:example.org": ["@bridge_owner:example.org"]}
    response = client.get("/api/config", headers=signed_connections_headers("bridge_owner"))
    assert response.status_code == 200


def test_personal_identity_can_derive_matrix_from_signed_email(
    connections_auth_client: Callable[..., TestClient],
    signed_connections_headers: Callable[[str], dict[str, str]],
) -> None:
    """Existing signed-email mapping remains a valid personal identity source."""
    client = connections_auth_client(
        MINDROOM_TRUSTED_UPSTREAM_JWT_MATRIX_USER_ID_CLAIM=None,
        MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE="@{localpart}:example.org",
    )
    response = client.get("/api/connections/identity", headers=signed_connections_headers("alice"))
    assert response.status_code == 200
    assert response.json()["matrix_user_id"] == "@alice:example.org"


def test_signed_administrator_can_load_dashboard_html(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    connections_auth_client: Callable[..., TestClient],
    signed_connections_headers: Callable[[str], dict[str, str]],
) -> None:
    """The enabled portal must preserve configured administrator HTML access."""
    (tmp_path / "index.html").write_text("administrator dashboard")
    monkeypatch.setattr(frontend, "ensure_frontend_dist_dir", lambda _runtime_paths: tmp_path)
    response = connections_auth_client().get("/", headers=signed_connections_headers("owner"))
    assert response.status_code == 200
    assert response.text == "administrator dashboard"
