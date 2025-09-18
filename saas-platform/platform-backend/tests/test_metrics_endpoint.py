"""Tests for the Prometheus metrics exposure."""

from fastapi.testclient import TestClient

from backend.metrics import record_admin_verification, record_auth_event, reset_security_metrics
from main import app


def test_metrics_endpoint_exposes_registered_metrics() -> None:
    """Ensure the /metrics endpoint returns our custom metrics."""

    reset_security_metrics()
    record_auth_event(actor="user", outcome="success")
    record_admin_verification("success")

    with TestClient(app) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    body = response.text
    assert "mindroom_auth_events_total" in body
    assert "mindroom_admin_verifications_total" in body
