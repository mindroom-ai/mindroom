"""Tests for the audit logging middleware request flow."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, Mock

import jwt
import pytest
from backend import auth_monitor, deps
from backend.middleware import audit_logging
from backend.middleware.audit_logging import AUDIT_BODY_MAX_BYTES, AuditLoggingMiddleware
from backend.routes import admin as admin_routes
from backend.utils.audit import REDACTED
from fastapi import FastAPI
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
def redaction_spy(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Record every redaction the middleware performs."""
    spy = Mock(side_effect=audit_logging.redact_audit_details)
    monkeypatch.setattr(audit_logging, "redact_audit_details", spy)
    return spy


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
def test_unauthenticated_mutation_does_no_body_work(audit_table: Mock, redaction_spy: Mock) -> None:
    """A rejected request to an admin route must not parse, redact, or audit its body."""
    body = json.dumps({"status": "a" * 4096})

    response = TestClient(app).put(
        "/admin/accounts/acc_1/status", content=body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 401
    redaction_spy.assert_not_called()
    audit_table.insert.assert_not_called()


def test_unrouted_mutation_does_no_body_work(audit_table: Mock, redaction_spy: Mock) -> None:
    """A request to a path no route serves must not parse, redact, or audit its body."""
    body = json.dumps({"a": "a" * 4096})

    response = TestClient(app).post(
        "/admin/no/such/route/here", content=body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code in {404, 405}
    redaction_spy.assert_not_called()
    audit_table.insert.assert_not_called()


def _app_with_mutation_routes(paths: list[str]) -> FastAPI:
    test_app = FastAPI()
    test_app.add_middleware(AuditLoggingMiddleware)
    for path in paths:
        test_app.add_api_route(path, lambda: {"ok": True}, methods=["POST"])
    return test_app


MOUNTED_MUTATION_PATHS = ["/my/instances/provision", "/system/provision", "/webhooks/stripe", "/stripe/checkout"]


@pytest.mark.parametrize("path", MOUNTED_MUTATION_PATHS)
def test_successful_mutations_are_audited_on_every_mounted_prefix(path: str, audit_table: Mock) -> None:
    """Tenant, provisioner, and webhook mutations produce audit rows, not only admin ones."""
    client = TestClient(_app_with_mutation_routes(MOUNTED_MUTATION_PATHS))

    response = client.post(path, json={"tier": "pro"})

    assert response.status_code == 200
    [row] = _inserted_rows(audit_table)
    assert row["details"]["path"] == path
    assert row["details"]["tier"] == "pro"


def test_oversized_body_is_audited_without_being_captured(audit_table: Mock, redaction_spy: Mock) -> None:
    """Bodies above the audit cap are recorded by a marker, never buffered or parsed for the audit row."""
    client = TestClient(_app_with_mutation_routes(["/my/instances/provision"]))
    body = json.dumps({"payload": "a" * (AUDIT_BODY_MAX_BYTES * 2)})

    response = client.post("/my/instances/provision", content=body, headers={"Content-Type": "application/json"})

    assert response.status_code == 200
    [row] = _inserted_rows(audit_table)
    assert row["details"]["body"] == "not-captured"
    assert "payload" not in row["details"]
    [(redacted,), _kwargs] = redaction_spy.call_args
    assert "payload" not in redacted


def _jwt_with_exp(expires_at: datetime) -> str:
    return jwt.encode({"sub": "user_123", "exp": int(expires_at.timestamp())}, "secret", algorithm="HS256")


@pytest.mark.usefixtures("real_auth")
def test_authenticated_mutation_is_attributed_to_the_account(
    monkeypatch: pytest.MonkeyPatch, audit_table: Mock
) -> None:
    """Audit rows name the verified account, its email, and the ingress-reported client IP."""
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
    token = _jwt_with_exp(datetime.now(UTC) + timedelta(minutes=5))

    response = TestClient(app).post(
        "/my/sso-cookie",
        json={"password": "pw-secret", "note": "kept"},
        headers={"Authorization": f"Bearer {token}", "X-Real-IP": "203.0.113.7"},
    )

    assert response.status_code == 200
    [row] = _inserted_rows(audit_table)
    assert row["account_id"] == "user_123"
    assert row["ip_address"] == "203.0.113.7"
    assert row["details"]["user_email"] == "user@example.test"
    assert row["details"]["password"] == REDACTED
    assert row["details"]["note"] == "kept"


@pytest.mark.usefixtures("real_auth")
def test_admin_mutation_is_attributed_to_the_admin(monkeypatch: pytest.MonkeyPatch, audit_table: Mock) -> None:
    """Admin mutations name the verified administrator in the middleware's audit row."""
    auth_user = Mock()
    auth_user.user.id = "admin_123"
    auth_user.user.email = "admin@example.test"
    auth_client = MagicMock()
    auth_client.auth.get_user.return_value = auth_user
    sb = MagicMock()
    sb.table().select().eq().single().execute.return_value = Mock(
        data={"is_admin": True, "status": "active", "deleted_at": None}
    )
    monkeypatch.setattr(deps, "_ensure_auth_client", lambda: auth_client)
    monkeypatch.setattr(deps, "ensure_supabase", lambda: sb)
    monkeypatch.setattr(admin_routes, "ensure_supabase", lambda: sb)

    response = TestClient(app).put(
        "/admin/accounts/acc_1/status", json={"status": "suspended"}, headers={"Authorization": "Bearer admin-token"}
    )

    assert response.status_code == 200
    [row] = _inserted_rows(audit_table)
    assert row["account_id"] == "admin_123"
    assert row["resource_type"] == "account"
    assert row["details"]["status"] == "suspended"
