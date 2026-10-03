"""Rendered Helm manifest checks for the web client chart."""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from tests.test_helm_instance_worker_isolation import _render_chart

_NGINX_IMAGE = "nginx:alpine"
_IMMUTABLE = "public, max-age=31536000, immutable"


def _client_nginx_conf(*set_args: str) -> str:
    docs = _render_chart(Path("cluster/k8s/client"), *set_args, release_name="mindroom-client")
    return next(doc["data"]["default.conf"] for doc in docs if "default.conf" in doc.get("data", {}))


@pytest.mark.parametrize(("base_path", "app_shell_location"), [("/", "location / {"), ("/chat", "location /chat/ {")])
def test_client_chart_lets_only_its_own_origin_frame_the_app_shell(base_path: str, app_shell_location: str) -> None:
    """A page framing the signed-in client could trick its user into clicking the client's controls."""
    nginx_conf = _client_nginx_conf(f"basePath={base_path}")
    app_shell = nginx_conf.split(app_shell_location, 1)[1].split("}", 1)[0]

    assert "add_header Content-Security-Policy \"frame-ancestors 'self'\" always;" in app_shell
    assert 'add_header X-Frame-Options "SAMEORIGIN" always;' in app_shell


def test_client_chart_serves_nested_build_files_from_their_own_path() -> None:
    """Nested build trees resolve to their own files, and only content-hashed files are cached as immutable.

    The bundled Element Call ships its own assets/ directory under public/element-call/.
    Resolving its hashed files against the app's top-level assets/ returned 404, and the immutable header on that 404
    pinned the broken call in the browser.
    """
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker is required to serve the rendered nginx config")
    if subprocess.run([docker, "info"], check=False, capture_output=True).returncode != 0:
        pytest.skip("Docker daemon is unavailable to serve the rendered nginx config")
    files = {
        "assets/app-1a2b.js": "app bundle",
        "public/locales/en.json": "translations",
        "public/element-call/index.html": "call page",
        "public/element-call/assets/call-3c4d.js": "call bundle",
    }
    expected = {
        "/assets/app-1a2b.js": ("200", _IMMUTABLE, "app bundle"),
        "/rooms/abc/assets/app-1a2b.js": ("200", _IMMUTABLE, "app bundle"),
        "/public/locales/en.json": ("200", "no-cache", "translations"),
        "/public/element-call/index.html": ("200", "no-cache", "call page"),
        "/public/element-call/assets/call-3c4d.js": ("200", _IMMUTABLE, "call bundle"),
        "/rooms/abc/public/element-call/assets/call-3c4d.js": ("200", _IMMUTABLE, "call bundle"),
        "/public/element-call/assets/missing-5e6f.js": ("404", "", None),
    }
    html = PurePosixPath("/usr/share/nginx/html")
    script = "\n".join(
        [
            "set -eu",
            "cat > /etc/nginx/conf.d/default.conf",
            *(
                f"mkdir -p {shlex.quote(str((html / name).parent))} && printf %s {shlex.quote(body)} > {html / name}"
                for name, body in files.items()
            ),
            # The image logs requests to stdout, which carries the responses.
            "nginx >&2",
            *(
                f"curl -s -o /tmp/body -w '%{{http_code}}\\t%header{{cache-control}}\\t' "
                f"{shlex.quote(f'http://127.0.0.1:8080{path}')} && tr -d '\\r\\n' < /tmp/body && echo"
                for path in expected
            ),
        ],
    )

    completed = subprocess.run(
        [docker, "run", "--rm", "-i", _NGINX_IMAGE, "sh", "-c", script],
        input=_client_nginx_conf("nginx.ipv6=false"),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    responses = [line.split("\t", 2) for line in completed.stdout.splitlines()]
    actual = {
        path: (status, cache_control, body if status == "200" else None)
        for path, (status, cache_control, body) in zip(expected, responses, strict=True)
    }
    assert actual == expected
