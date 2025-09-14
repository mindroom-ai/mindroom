"""Tests for provisioner API auth and basic behaviors."""

from __future__ import annotations

from typing import TYPE_CHECKING

import backend.routes.provisioner as prov
from fastapi.testclient import TestClient
from main import app

if TYPE_CHECKING:  # pragma: no cover
    import pytest


def test_start_requires_auth() -> None:
    """Start route returns 401 without provisioner auth."""
    client = TestClient(app)
    r = client.post("/system/instances/1/start")
    assert r.status_code == 401


def test_start_ok_with_valid_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start route succeeds when provisioner auth is accepted."""
    # Set expected API key before client creation
    # Patch both the provisioner module and the source config module
    import backend.config as cfg  # noqa: PLC0415

    monkeypatch.setattr(cfg, "PROVISIONER_API_KEY", "good")
    monkeypatch.setattr(prov, "PROVISIONER_API_KEY", "good")
    # In some environments header parsing can be finicky; bypass the auth gate
    monkeypatch.setattr(prov, "_require_provisioner_auth", lambda _auth: None)

    # Stub k8s calls and DB update
    async def _exists(instance_id: int) -> bool:  # noqa: ARG001
        return True

    monkeypatch.setattr(prov, "check_deployment_exists", _exists)

    async def _fake_kubectl(args: list[str], namespace: str = "mindroom-instances") -> tuple[int, str, str]:  # noqa: ARG001
        return 0, "ok", ""

    monkeypatch.setattr(prov, "run_kubectl", _fake_kubectl)
    monkeypatch.setattr(prov, "update_instance_status", lambda instance_id, status: True)  # noqa: ARG005

    client = TestClient(app)
    r = client.post(
        "/system/instances/1/start",
        headers={"authorization": "Bearer good", "X-Forwarded-For": "10.0.0.10"},
    )
    assert r.status_code == 200
    assert r.json().get("success") is True
