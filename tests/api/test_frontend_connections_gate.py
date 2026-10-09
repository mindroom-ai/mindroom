"""Static-serving gate for the personal egress page and its assets."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindroom import constants
from mindroom.api import config_lifecycle, frontend, main

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_TRUSTED_HEADERS = {"X-Trusted-User": "alice"}


@pytest.fixture
def gate_client(
    monkeypatch: pytest.MonkeyPatch,
    temp_config_file: Path,
    tmp_path: Path,
) -> Callable[..., TestClient]:
    """Build an app serving a fake dashboard dist with a dedicated connections bundle."""
    dist = tmp_path / "dist"
    portal = dist / "connections"
    (portal / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("administrator dashboard")
    (portal / "index.html").write_text("connections bundle")
    (portal / "assets" / "portal.js").write_text("portal asset")
    monkeypatch.setattr(frontend, "ensure_frontend_dist_dir", lambda _runtime_paths: dist)

    def client(*, portal_agent: str | None, trusted_upstream: bool) -> TestClient:
        env = {
            "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true" if trusted_upstream else "false",
            "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER": "X-Trusted-User",
        }
        if portal_agent is not None:
            env["MINDROOM_CONNECTIONS_AGENT"] = portal_agent
        runtime_paths = constants.resolve_primary_runtime_paths(
            config_path=temp_config_file,
            storage_path=tmp_path / "storage",
            process_env=env,
        )
        app = FastAPI()
        main.initialize_api_app(app, runtime_paths)
        config_lifecycle.load_config_into_app(runtime_paths, app)
        app.include_router(frontend.router)
        return TestClient(app, base_url="http://localhost")

    return client


@pytest.mark.parametrize(
    "path",
    [
        "/connections/egress",
        "/connections/egress/",
        "/connections/egress/agents",
        "/connections/assets/portal.js",
    ],
)
def test_trusted_upstream_serves_egress_page_without_portal(
    path: str,
    gate_client: Callable[..., TestClient],
) -> None:
    """The egress page and its assets load for trusted users even when the portal is disabled."""
    response = gate_client(portal_agent=None, trusted_upstream=True).get(path, headers=_TRUSTED_HEADERS)
    assert response.status_code == 200
    assert response.text in {"connections bundle", "portal asset"}


@pytest.mark.parametrize("portal_agent", [None, "", "   "])
@pytest.mark.parametrize("path", ["/connections", "/connections/", "/connections/nested", "/connections/egressx"])
def test_other_connections_paths_stay_gated_by_portal(
    portal_agent: str | None,
    path: str,
    gate_client: Callable[..., TestClient],
) -> None:
    """Only the egress page and assets bypass the MINDROOM_CONNECTIONS_AGENT gate."""
    response = gate_client(portal_agent=portal_agent, trusted_upstream=True).get(path, headers=_TRUSTED_HEADERS)
    assert response.status_code == 404
    assert response.json()["detail"] == "Connections are not enabled"


@pytest.mark.parametrize("path", ["/connections/egress", "/connections/egress/", "/connections/assets/portal.js"])
def test_egress_page_is_not_served_without_trusted_upstream_or_portal(
    path: str,
    gate_client: Callable[..., TestClient],
) -> None:
    """With neither the portal nor trusted upstream auth enabled, the egress page is a 404."""
    response = gate_client(portal_agent=None, trusted_upstream=False).get(path)
    assert response.status_code == 404
    assert response.json()["detail"] == "Connections are not enabled"
