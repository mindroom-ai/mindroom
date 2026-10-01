"""Rendered Helm manifest checks for the web client chart."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_helm_instance_worker_isolation import _render_chart


@pytest.mark.parametrize(("base_path", "app_shell_location"), [("/", "location / {"), ("/chat", "location /chat/ {")])
def test_client_chart_lets_only_its_own_origin_frame_the_app_shell(base_path: str, app_shell_location: str) -> None:
    """A page framing the signed-in client could trick its user into clicking the client's controls."""
    docs = _render_chart(Path("cluster/k8s/client"), f"basePath={base_path}", release_name="mindroom-client")
    nginx_conf = next(doc["data"]["default.conf"] for doc in docs if "default.conf" in doc.get("data", {}))
    app_shell = nginx_conf.split(app_shell_location, 1)[1].split("}", 1)[0]

    assert "add_header Content-Security-Policy \"frame-ancestors 'self'\" always;" in app_shell
    assert 'add_header X-Frame-Options "SAMEORIGIN" always;' in app_shell
