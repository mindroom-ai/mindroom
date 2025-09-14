"""Rate limit tests for provisioner endpoints.

These tests ensure SlowAPI limits are enforced on provisioner routes
without touching Kubernetes or the database by stubbing dependencies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import backend.routes.provisioner as prov
from fastapi.testclient import TestClient
from main import app

if TYPE_CHECKING:  # pragma: no cover
    import pytest


def _setup_provisioner_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub external calls and set a valid API key."""
    import backend.config as cfg  # noqa: PLC0415

    monkeypatch.setattr(cfg, "PROVISIONER_API_KEY", "good")
    monkeypatch.setattr(prov, "PROVISIONER_API_KEY", "good")
    # Avoid auth-related flakiness by skipping auth check
    monkeypatch.setattr(prov, "_require_provisioner_auth", lambda _auth: None)

    async def _exists(instance_id: int) -> bool:  # noqa: ARG001
        return True

    monkeypatch.setattr(prov, "check_deployment_exists", _exists)

    async def _fake_kubectl(args: list[str], namespace: str = "mindroom-instances") -> tuple[int, str, str]:  # noqa: ARG001
        return 0, "ok", ""

    monkeypatch.setattr(prov, "run_kubectl", _fake_kubectl)
    monkeypatch.setattr(prov, "update_instance_status", lambda instance_id, status: True)  # noqa: ARG005

    # Also stub helm for uninstall route
    async def _fake_helm(args: list[str]) -> tuple[int, str, str]:  # noqa: ARG001
        return 0, "ok", ""

    monkeypatch.setattr(prov, "run_helm", _fake_helm)


def test_start_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """10 requests allowed; 11th returns 429."""
    _setup_provisioner_stubs(monkeypatch)
    client = TestClient(app)

    statuses: list[int] = []
    headers = {"authorization": "Bearer good", "X-Forwarded-For": "10.0.0.1"}
    for _ in range(11):
        r = client.post("/system/instances/2/start", headers=headers)
        statuses.append(r.status_code)

    assert all(code == 200 for code in statuses[:10])
    assert statuses[10] == 429


def test_uninstall_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """2 requests allowed; 3rd returns 429."""
    _setup_provisioner_stubs(monkeypatch)
    client = TestClient(app)

    headers = {"authorization": "Bearer good", "X-Forwarded-For": "10.0.0.2"}
    r1 = client.delete("/system/instances/1/uninstall", headers=headers)
    r2 = client.delete("/system/instances/1/uninstall", headers=headers)
    r3 = client.delete("/system/instances/1/uninstall", headers=headers)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429
