"""Tests for the Prometheus metrics exposure."""

from fastapi.testclient import TestClient

from backend.metrics import record_admin_verification, record_auth_event, reset_security_metrics
from main import app


def test_metrics_endpoint_exposes_registered_metrics_to_internal_scrapes() -> None:
    """Internal service scrapes should still receive custom metrics."""

    reset_security_metrics()
    record_auth_event(actor="user", outcome="success")
    record_admin_verification("success")

    with TestClient(app) as client:
        response = client.get("/metrics", headers={"host": "platform-backend:8000"})

    assert response.status_code == 200
    body = response.text
    assert "mindroom_auth_events_total" in body
    assert "mindroom_admin_verifications_total" in body


def test_metrics_endpoint_accepts_internal_service_host_with_runtime_port() -> None:
    """Host header port changes must not block in-cluster service scrapes."""

    with TestClient(app) as client:
        response = client.get("/metrics", headers={"host": "platform-backend:9090"})

    assert response.status_code == 200


def test_metrics_endpoint_rejects_public_host() -> None:
    """Public ingress hosts must not expose Prometheus samples."""

    with TestClient(app) as client:
        response = client.get("/metrics", headers={"host": "api.mindroom.chat"})

    assert response.status_code == 404


def test_production_hides_fastapi_documentation() -> None:
    """Production should not expose generated API docs or schema."""

    with TestClient(app) as client:
        docs_response = client.get("/docs", headers={"host": "api.mindroom.chat"})
        openapi_response = client.get("/openapi.json", headers={"host": "api.mindroom.chat"})

    assert docs_response.status_code == 404
    assert openapi_response.status_code == 404
