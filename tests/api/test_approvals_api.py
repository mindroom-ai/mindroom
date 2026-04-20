"""Tests that the deprecated approvals API surface is no longer mounted."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def test_approvals_routes_are_not_mounted(test_client: TestClient) -> None:
    """The standalone API should not expose approvals REST or WebSocket routes."""
    from mindroom.api import main  # noqa: PLC0415

    route_paths = {getattr(route, "path", None) for route in main.app.routes}

    assert "/api/approvals" not in route_paths
    assert "/api/approvals/ws" not in route_paths

    response = test_client.get("/api/approvals")

    assert response.status_code == 404
