"""Hosted-instance dashboard login tickets."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from backend import deps
from backend.deps import Limiter, get_remote_address, limiter
from backend.routes import matrix_oidc, sso
from backend.services import provisioner_service
from fastapi import HTTPException
from fastapi.testclient import TestClient
from main import app

_OWNED_INSTANCE_ID = "1"


@pytest.fixture(autouse=True)
def owned_instance_lookups(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Configure one platform domain, one owned instance, and a deterministic signing root."""
    monkeypatch.setattr(sso, "PLATFORM_DOMAIN", "mindroom.chat")
    monkeypatch.setattr(sso, "INSTANCE_BASE_DOMAIN", "mindroom.chat")
    monkeypatch.setattr(provisioner_service, "INSTANCE_CREDENTIALS_ENCRYPTION_SECRET", "root-secret")
    monkeypatch.setattr(
        sso,
        "verify_user",
        AsyncMock(
            return_value={"user_id": "user-123", "account_id": "user-123", "email": "alice@example.com"},
        ),
    )
    lookups: list[tuple[str, str]] = []

    def _get_owned_instance(_sb: object, instance_id: str, account_id: str) -> dict[str, str] | None:
        lookups.append((instance_id, account_id))
        if instance_id != _OWNED_INSTANCE_ID:
            return None
        return {"instance_id": instance_id, "account_id": account_id, "subscription_id": "sub-123"}

    monkeypatch.setattr(sso.instances_data, "get_owned_instance", _get_owned_instance)
    _use_subscription(monkeypatch, {"id": "sub-123", "tier": "byok", "status": "active"})
    app.state.limiter = Limiter(key_func=get_remote_address)
    app.state.limiter.reset()
    limiter.reset()
    return lookups


def _use_subscription(monkeypatch: pytest.MonkeyPatch, subscription: dict[str, str]) -> None:
    subscription_query = MagicMock()
    subscription_query.select.return_value = subscription_query
    subscription_query.eq.return_value = subscription_query
    subscription_query.limit.return_value = subscription_query
    subscription_query.execute.return_value = Mock(data=[subscription])
    supabase = Mock(table=Mock(return_value=subscription_query))
    monkeypatch.setattr(sso, "ensure_supabase", lambda: supabase)


def _authorize(client: TestClient, redirect_to: str, *, cookie: str | None = "supabase-access-token"):  # noqa: ANN202
    if cookie is not None:
        client.cookies.set(sso.SSO_COOKIE_NAME, cookie)
    return client.get("/instance-sso/authorize", params={"redirect_to": redirect_to}, follow_redirects=False)


def test_instance_sso_redirects_anonymous_users_to_platform_login() -> None:
    """Without the API-host cookie the browser signs in first and returns to this endpoint."""
    client = TestClient(app)

    response = _authorize(client, "https://1.mindroom.chat/agents", cookie=None)

    assert response.status_code == 307
    location = urlparse(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == "https://app.mindroom.chat/auth/login"
    return_to = parse_qs(location.query)["redirect_to"][0]
    assert (
        return_to
        == "https://api.mindroom.chat/instance-sso/authorize?redirect_to=https%3A%2F%2F1.mindroom.chat%2Fagents"
    )


def test_instance_sso_issues_ticket_bound_to_the_owned_instance(
    owned_instance_lookups: list[tuple[str, str]],
) -> None:
    """The ticket is short-lived, single-purpose, and signed with only that instance's key."""
    client = TestClient(app)

    response = _authorize(client, "https://1.mindroom.chat/agents?tab=tools")

    assert response.status_code == 307
    assert owned_instance_lookups == [("1", "user-123")]
    location = urlparse(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == "https://1.mindroom.chat/api/auth/platform-sso"
    query = parse_qs(location.query)
    assert query["next"] == ["/agents?tab=tools"]
    ticket = query["ticket"][0]
    assert ticket != "supabase-access-token"

    claims = jwt.decode(
        ticket,
        provisioner_service.instance_platform_sso_secret("1"),
        algorithms=["HS256"],
        audience="https://1.mindroom.chat",
    )
    assert claims["typ"] == sso.INSTANCE_SSO_TICKET_TYPE
    assert claims["sub"] == "user-123"
    assert claims["email"] == "alice@example.com"
    assert claims["exp"] - claims["iat"] == sso.INSTANCE_SSO_TICKET_TTL_SECONDS
    assert claims["jti"]
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(
            ticket,
            provisioner_service.instance_platform_sso_secret("2"),
            algorithms=["HS256"],
            audience="https://1.mindroom.chat",
        )


def test_instance_sso_refuses_instances_the_user_does_not_own() -> None:
    """Another customer's instance never receives a ticket naming this user."""
    client = TestClient(app)

    response = _authorize(client, "https://2.mindroom.chat/")

    assert response.status_code == 403
    assert "location" not in response.headers


def test_instance_sso_refuses_instances_whose_subscription_disallows_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dashboard login follows the same subscription entitlement as hosted Matrix login."""
    _use_subscription(monkeypatch, {"id": "sub-123", "tier": "starter", "status": "cancelled"})
    client = TestClient(app)

    response = _authorize(client, "https://1.mindroom.chat/")

    assert response.status_code == 402
    assert "location" not in response.headers


@pytest.mark.parametrize(
    "redirect_to",
    [
        "http://1.mindroom.chat/",
        "https://1.api.mindroom.chat/",
        "https://1.matrix.mindroom.chat/",
        "https://mindroom.chat/",
        "https://1.mindroom.chat.evil.example/",
        "https://1.evil-mindroom.chat/",
        "https://user@1.mindroom.chat/",
        "https://1.mindroom.chat:8443/",
        "https://1.mindroom.chat\\@evil.example/",
        "https://evil.example#@1.mindroom.chat/",
        "https://1.mindroom.chat./",
        "https://01.mindroom.chat/",
        "https://app.mindroom.chat/",
        "https://[::1/",
        "/dashboard",
    ],
)
def test_instance_sso_rejects_non_dashboard_targets(
    redirect_to: str,
    owned_instance_lookups: list[tuple[str, str]],
) -> None:
    """Tickets are only delivered to an instance dashboard origin under the instance base domain."""
    client = TestClient(app)

    response = _authorize(client, redirect_to)

    assert response.status_code == 400
    assert owned_instance_lookups == []


def test_instance_sso_sends_unverified_cookies_to_platform_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """An expired or revoked platform cookie restarts platform login instead of issuing a ticket."""
    monkeypatch.setattr(sso, "verify_user", AsyncMock(side_effect=HTTPException(status_code=401)))
    client = TestClient(app)

    response = _authorize(client, "https://1.mindroom.chat/")

    assert response.status_code == 307
    assert response.headers["location"].startswith("https://app.mindroom.chat/auth/login?")


def test_instance_sso_ticket_is_not_a_platform_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ticket captured on an instance is rejected by the platform API and by Matrix OIDC."""
    location = urlparse(_authorize(TestClient(app), "https://1.mindroom.chat/").headers["location"])
    ticket = parse_qs(location.query)["ticket"][0]

    class _SupabaseAuth:
        @staticmethod
        def get_user(_token: str) -> None:
            return None  # Supabase knows no session for a token it did not issue.

    monkeypatch.setattr(deps, "auth_client", Mock(auth=_SupabaseAuth()))
    monkeypatch.setattr(sso, "verify_user", deps.verify_user)
    monkeypatch.setattr(matrix_oidc, "MATRIX_OIDC_ENABLED", True)
    monkeypatch.setattr(matrix_oidc, "MATRIX_OIDC_CLIENT_ID", "mindroom-synapse")
    monkeypatch.setattr(matrix_oidc, "PLATFORM_DOMAIN", "mindroom.chat")
    monkeypatch.setattr(matrix_oidc, "INSTANCE_BASE_DOMAIN", "mindroom.chat")
    client = TestClient(app)

    # Unique client IPs keep auth-failure monitoring from other tests out of these requests.
    account = client.get("/my/account", headers={"authorization": f"Bearer {ticket}", "X-Forwarded-For": "10.9.0.1"})
    client.cookies.set(sso.SSO_COOKIE_NAME, ticket)
    oidc = client.get(
        "/matrix-oidc/authorize",
        params={
            "response_type": "code",
            "client_id": "mindroom-synapse",
            "redirect_uri": "https://1.matrix.mindroom.chat/_synapse/client/oidc/callback",
            "scope": "openid",
            "state": "state-123",
        },
        headers={"X-Forwarded-For": "10.9.0.2"},
        follow_redirects=False,
    )

    assert account.status_code == 401
    assert oidc.status_code == 307
    assert oidc.headers["location"].startswith("https://app.mindroom.chat/auth/login?")
