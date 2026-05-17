"""Matrix OIDC bridge for tenant Synapse login."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock, MagicMock, Mock

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from main import app


def _test_private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


def _patch_oidc(monkeypatch):
    import backend.routes.matrix_oidc as matrix_oidc

    monkeypatch.setattr(matrix_oidc, "MATRIX_OIDC_ENABLED", True)
    monkeypatch.setattr(matrix_oidc, "MATRIX_OIDC_CLIENT_ID", "mindroom-synapse")
    monkeypatch.setattr(matrix_oidc, "MATRIX_OIDC_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(matrix_oidc, "MATRIX_OIDC_PRIVATE_KEY", _test_private_key())
    monkeypatch.setattr(matrix_oidc, "MATRIX_OIDC_KEY_ID", "test-key")
    monkeypatch.setattr(matrix_oidc, "PLATFORM_DOMAIN", "mindroom.chat")
    monkeypatch.setattr(matrix_oidc, "INSTANCE_BASE_DOMAIN", "mindroom.chat")
    return matrix_oidc


def test_matrix_oidc_discovery_advertises_platform_issuer(monkeypatch) -> None:
    _patch_oidc(monkeypatch)
    client = TestClient(app)

    response = client.get(
        "/matrix-oidc/.well-known/openid-configuration",
        headers={"host": "api.mindroom.chat"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["issuer"] == "https://api.mindroom.chat/matrix-oidc"
    assert body["authorization_endpoint"] == "https://api.mindroom.chat/matrix-oidc/authorize"
    assert body["token_endpoint_auth_methods_supported"] == ["client_secret_basic", "client_secret_post"]
    assert body["id_token_signing_alg_values_supported"] == ["RS256"]


def test_matrix_oidc_authorize_redirects_anonymous_users_to_platform_login(monkeypatch) -> None:
    _patch_oidc(monkeypatch)
    client = TestClient(app)
    params = {
        "response_type": "code",
        "client_id": "mindroom-synapse",
        "redirect_uri": "https://1.matrix.mindroom.chat/_synapse/client/oidc/callback",
        "scope": "openid profile email",
        "state": "state-123",
        "nonce": "nonce-123",
    }

    response = client.get(
        "/matrix-oidc/authorize",
        params=params,
        headers={"host": "api.mindroom.chat"},
        follow_redirects=False,
    )

    assert response.status_code == 307
    location = response.headers["location"]
    assert location.startswith("https://app.mindroom.chat/auth/login?")
    assert parse_qs(urlparse(location).query)["redirect_to"][0].startswith(
        "https://api.mindroom.chat/matrix-oidc/authorize?"
    )


def test_matrix_oidc_code_flow_maps_platform_user_to_owned_tenant(monkeypatch) -> None:
    matrix_oidc = _patch_oidc(monkeypatch)
    verify_user = AsyncMock(
        return_value={
            "user_id": "user-123",
            "account_id": "account-123",
            "email": "alice@example.com",
            "account": {"full_name": "Alice Example"},
        }
    )
    monkeypatch.setattr(matrix_oidc, "verify_user", verify_user)

    instance_query = MagicMock()
    instance_query.select.return_value = instance_query
    instance_query.eq.return_value = instance_query
    instance_query.limit.return_value = instance_query
    instance_query.execute.return_value = Mock(
        data=[{"instance_id": 1, "subscription_id": "sub-123", "account_id": "account-123"}]
    )

    subscription_query = MagicMock()
    subscription_query.select.return_value = subscription_query
    subscription_query.eq.return_value = subscription_query
    subscription_query.limit.return_value = subscription_query
    subscription_query.execute.return_value = Mock(data=[{"id": "sub-123", "tier": "starter", "status": "active"}])

    supabase = MagicMock()
    supabase.table.side_effect = [instance_query, subscription_query]
    monkeypatch.setattr(matrix_oidc, "ensure_supabase", lambda: supabase)

    client = TestClient(app)
    response = client.get(
        "/matrix-oidc/authorize",
        params={
            "response_type": "code",
            "client_id": "mindroom-synapse",
            "redirect_uri": "https://1.matrix.mindroom.chat/_synapse/client/oidc/callback",
            "scope": "openid profile email",
            "state": "state-123",
            "nonce": "nonce-123",
        },
        cookies={"mindroom_jwt": "supabase-access-token"},
        headers={"host": "api.mindroom.chat"},
        follow_redirects=False,
    )

    assert response.status_code == 307
    redirect = urlparse(response.headers["location"])
    assert redirect.scheme == "https"
    assert redirect.netloc == "1.matrix.mindroom.chat"
    query = parse_qs(redirect.query)
    assert query["state"] == ["state-123"]
    code = query["code"][0]

    token_response = client.post(
        "/matrix-oidc/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://1.matrix.mindroom.chat/_synapse/client/oidc/callback",
            "client_id": "mindroom-synapse",
            "client_secret": "client-secret",
        },
        headers={"host": "api.mindroom.chat"},
    )

    assert token_response.status_code == 200
    tokens = token_response.json()
    claims = jwt.decode(tokens["id_token"], options={"verify_signature": False})
    assert claims["iss"] == "https://api.mindroom.chat/matrix-oidc"
    assert claims["aud"] == "mindroom-synapse"
    assert claims["sub"] == "user-123"
    assert claims["email"] == "alice@example.com"
    assert claims["name"] == "Alice Example"
    assert claims["nonce"] == "nonce-123"

    userinfo_response = client.get(
        "/matrix-oidc/userinfo",
        headers={"authorization": f"Bearer {tokens['access_token']}", "host": "api.mindroom.chat"},
    )
    assert userinfo_response.status_code == 200
    assert userinfo_response.json()["email"] == "alice@example.com"
