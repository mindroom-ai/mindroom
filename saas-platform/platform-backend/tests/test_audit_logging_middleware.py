"""Tests for the audit logging middleware request flow."""

from __future__ import annotations

import ipaddress
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, Mock

import jwt
import pytest
from backend import auth_monitor, deps
from backend.middleware import audit_logging
from backend.middleware.audit_logging import AuditLoggingMiddleware
from backend.routes import admin as admin_routes
from backend.utils.audit import AuditActor, record_audit_actor
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from main import app


@pytest.fixture
def audit_table(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Capture audit rows the middleware writes."""
    table = Mock()
    table.insert.return_value.execute.return_value = Mock()
    supabase = Mock()
    supabase.table.return_value = table
    monkeypatch.setattr(audit_logging, "supabase", supabase)
    return table


@pytest.fixture
def real_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the real auth dependencies, whatever overrides and failure counters earlier tests left behind."""
    monkeypatch.setattr(app, "dependency_overrides", {})
    monkeypatch.setattr(auth_monitor, "failed_attempts", defaultdict(list))
    monkeypatch.setattr(auth_monitor, "blocked_ips", {})
    deps._auth_cache.clear()
    yield
    deps._auth_cache.clear()


def _inserted_rows(audit_table: Mock) -> list[dict]:
    return [call.args[0] for call in audit_table.insert.call_args_list]


@pytest.mark.usefixtures("real_auth")
@pytest.mark.parametrize(
    ("method", "path", "status_code"),
    [
        pytest.param("PUT", "/admin/accounts/acc_1/status", 401, id="unauthenticated"),
        pytest.param("POST", "/admin/no/such/route/here", 404, id="unrouted"),
        pytest.param("POST", "/my/instances/provision/", 307, id="redirected-before-authentication"),
        pytest.param("POST", "/webhooks/stripe/", 307, id="redirected-webhook"),
    ],
)
def test_requests_no_route_accepted_are_not_audited(
    method: str, path: str, status_code: int, audit_table: Mock
) -> None:
    """Only a 2xx answer from a route produces an audit row."""
    response = TestClient(app).request(method, path, json={"a": "a" * 4096}, follow_redirects=False)

    assert response.status_code == status_code
    audit_table.insert.assert_not_called()


def _app_with_mutation_routes(paths: list[str], actor: AuditActor | None = None) -> FastAPI:
    async def accept(request: Request) -> dict[str, int]:
        if actor is not None:
            record_audit_actor(request, actor)
        return {"received": len(await request.body())}

    test_app = FastAPI()
    test_app.add_middleware(AuditLoggingMiddleware)
    for path in paths:
        test_app.add_api_route(path, accept, methods=["POST"])
    return test_app


MOUNTED_MUTATION_PATHS = ["/my/instances/provision", "/system/provision", "/webhooks/stripe", "/stripe/checkout"]
ACCOUNT = AuditActor(account_id="user_123", email="user@example.test")


@pytest.mark.parametrize("actor", [pytest.param(ACCOUNT, id="authenticated"), pytest.param(None, id="machine")])
@pytest.mark.parametrize("path", MOUNTED_MUTATION_PATHS)
def test_accepted_mutations_are_audited_with_request_metadata_only(
    path: str, actor: AuditActor | None, audit_table: Mock
) -> None:
    """Tenant, provisioner, and webhook mutations are audited, and their bodies never reach the row."""
    body = json.dumps({"customer_email": "payer@example.test", "payload": "a" * 100_000})

    response = TestClient(
        _app_with_mutation_routes(MOUNTED_MUTATION_PATHS, actor=actor), client=("203.0.113.7", 50000)
    ).post(path, content=body, headers={"Content-Type": "application/json"})

    assert response.json() == {"received": len(body)}
    [row] = _inserted_rows(audit_table)
    expected_details = {"method": "POST", "path": path, "status_code": 200}
    if actor is not None:
        expected_details["user_email"] = actor.email
        assert row["account_id"] == actor.account_id
    else:
        assert "account_id" not in row
    assert row["details"] == expected_details
    assert row["ip_address"] == "203.0.113.7"


@pytest.mark.usefixtures("real_auth")
def test_anonymous_route_is_audited_without_its_body(audit_table: Mock) -> None:
    """A route that answers anyone produces an unattributed metadata row."""
    response = TestClient(app).request("DELETE", "/my/sso-cookie", json={"payload": "a" * 4096})

    assert response.status_code == 200
    [row] = _inserted_rows(audit_table)
    assert "account_id" not in row
    assert row["details"] == {"method": "DELETE", "path": "/my/sso-cookie", "status_code": 200}


def _jwt_with_exp(expires_at: datetime) -> str:
    return jwt.encode({"sub": "user_123", "exp": int(expires_at.timestamp())}, "secret", algorithm="HS256")


@pytest.mark.usefixtures("real_auth")
def test_authenticated_mutation_is_attributed_to_the_account(
    monkeypatch: pytest.MonkeyPatch, audit_table: Mock
) -> None:
    """Audit rows name the verified account, its email, and the client IP the trusted ingress reports."""
    auth_user = Mock()
    auth_user.user.id = "user_123"
    auth_user.user.email = "user@example.test"
    auth_client = MagicMock()
    auth_client.auth.get_user.return_value = auth_user
    sb = MagicMock()
    sb.table().select().eq().single().execute.return_value = Mock(
        data={"id": "user_123", "email": "user@example.test", "status": "active", "deleted_at": None}
    )
    monkeypatch.setattr(deps, "_ensure_auth_client", lambda: auth_client)
    monkeypatch.setattr(deps, "ensure_supabase", lambda: sb)
    monkeypatch.setattr(deps, "TRUSTED_PROXY_NETWORKS", (ipaddress.ip_network("10.42.0.0/16"),))
    token = _jwt_with_exp(datetime.now(UTC) + timedelta(minutes=5))

    response = TestClient(app, client=("10.42.0.7", 50000)).post(
        "/my/sso-cookie",
        json={"password": "pw-secret"},
        headers={"Authorization": f"Bearer {token}", "X-Real-IP": "203.0.113.7"},
    )

    assert response.status_code == 200
    [row] = _inserted_rows(audit_table)
    assert row["account_id"] == "user_123"
    assert row["ip_address"] == "203.0.113.7"
    assert row["details"] == {
        "method": "POST",
        "path": "/my/sso-cookie",
        "status_code": 200,
        "user_email": "user@example.test",
    }


@pytest.mark.usefixtures("real_auth")
def test_admin_mutation_is_attributed_and_its_data_audited_by_the_route(
    monkeypatch: pytest.MonkeyPatch, audit_table: Mock
) -> None:
    """The middleware row names the administrator, and the admin route's own entry keeps the request data."""
    auth_user = Mock()
    auth_user.user.id = "admin_123"
    auth_user.user.email = "admin@example.test"
    auth_client = MagicMock()
    auth_client.auth.get_user.return_value = auth_user
    sb = MagicMock()
    sb.table().select().eq().single().execute.return_value = Mock(
        data={"is_admin": True, "status": "active", "deleted_at": None}
    )
    route_audit = Mock()
    monkeypatch.setattr(deps, "_ensure_auth_client", lambda: auth_client)
    monkeypatch.setattr(deps, "ensure_supabase", lambda: sb)
    monkeypatch.setattr(admin_routes, "ensure_supabase", lambda: sb)
    monkeypatch.setattr(admin_routes, "create_audit_log", route_audit)

    response = TestClient(app).put(
        "/admin/accounts/acc_1/status",
        json={"status": "suspended", "reason": "abuse"},
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 200
    [row] = _inserted_rows(audit_table)
    assert row["account_id"] == "admin_123"
    assert row["resource_type"] == "account"
    assert row["details"]["method"] == "PUT"
    assert "status" not in row["details"]
    assert route_audit.call_args.kwargs["details"] == {"status": "suspended", "reason": "abuse"}
