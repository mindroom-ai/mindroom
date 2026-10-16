"""Tests that generic credential API routes reject egress_* services."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/credentials/egress_github", None),
        ("POST", "/api/credentials/egress_github", {"credentials": {"api_key": "test"}}),
        ("GET", "/api/credentials/egress_github/api-key?include_value=true", None),
        ("POST", "/api/credentials/egress_github/api-key", {"api_key": "test", "service": "egress_github"}),
        ("DELETE", "/api/credentials/egress_github", None),
        ("POST", "/api/credentials/egress_github/copy-from/other_service", None),
        ("POST", "/api/credentials/other_service/copy-from/egress_github", None),
        ("GET", "/api/credentials/egress_github/status", None),
        ("POST", "/api/credentials/egress_github/test", None),
    ],
)
def test_generic_routes_reject_egress_services(
    test_client: TestClient,
    method: str,
    path: str,
    body: dict | None,
) -> None:
    """All generic credential routes should reject egress_* services with 400."""
    kwargs = {"json": body} if body else {}
    response = test_client.request(method, path, **kwargs)
    assert response.status_code == 400
    assert "Egress broker secrets" in response.json()["detail"]
