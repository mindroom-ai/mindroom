"""Rendered Helm manifest checks for the web client chart."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from tests.test_helm_instance_worker_isolation import _render_chart, _run_helm_template, _values_files

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


@pytest.mark.parametrize("base_path", ["/", "/chat", "/assets", "/public"])
def test_client_chart_serves_nested_build_files_from_their_own_path(base_path: str) -> None:
    """Element Call resolves to its own files, other build files resolve as before, and 404s are not immutable.

    The bundled Element Call ships its own assets/ directory under public/element-call/.
    Resolving its hashed files against the app's top-level assets/ returned 404, and the immutable header on that 404
    pinned the broken call in the browser.
    Base paths and route segments named assets or public must still resolve to the top-level build files.
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
    prefix = base_path.rstrip("/")
    expected = {
        f"{prefix}{path}": response
        for path, response in {
            "/assets/app-1a2b.js": ("200", _IMMUTABLE, "app bundle"),
            "/rooms/abc/assets/app-1a2b.js": ("200", _IMMUTABLE, "app bundle"),
            "/rooms/public/assets/app-1a2b.js": ("200", _IMMUTABLE, "app bundle"),
            "/rooms/assets/public/locales/en.json": ("200", _IMMUTABLE, "translations"),
            "/public/locales/en.json": ("200", _IMMUTABLE, "translations"),
            "/public/element-call/index.html": ("200", "no-cache", "call page"),
            "/public/element-call/assets/call-3c4d.js": ("200", _IMMUTABLE, "call bundle"),
            "/rooms/abc/public/element-call/assets/call-3c4d.js": ("200", _IMMUTABLE, "call bundle"),
            "/public/element-call/assets/missing-5e6f.js": ("404", "", None),
            "/assets/missing-5e6f.js": ("404", "", None),
        }.items()
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
        input=_client_nginx_conf(f"basePath={base_path}", "nginx.ipv6=false"),
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


# Historical uppercase localparts, ports, and IPv6 server names are valid Matrix user IDs the client accepts.
_BUG_REPORT_ADMINS = ["@admin:chat.example.com", "@Ops=Team/1+x:chat.example.com:8448", "@admin:[2001:db8::1]"]
_MATRIX_RTC = """
matrixRTC:
  enabled: true
  livekitServiceUrl: https://chat.example.com/livekit/jwt
"""


def _matrix_client_well_known(tmp_path: Path, values: str) -> dict[str, Any] | None:
    docs = _render_chart(
        Path("cluster/k8s/client"),
        release_name="mindroom-client",
        values_files=_values_files(tmp_path, values),
    )
    nginx_conf = next(doc["data"]["default.conf"] for doc in docs if "default.conf" in doc.get("data", {}))
    _, found, location = nginx_conf.partition("location = /.well-known/matrix/client {")
    if not found:
        return None
    body = re.search(r"return 200 '([^']*)';", location)
    assert body is not None
    return json.loads(body[1])


@pytest.mark.parametrize("rtc", [False, True], ids=["no-rtc", "rtc"])
@pytest.mark.parametrize("admins", [False, True], ids=["no-admins", "admins"])
def test_client_chart_publishes_bug_report_admins_in_matrix_client_discovery(
    tmp_path: Path,
    *,
    rtc: bool,
    admins: bool,
) -> None:
    """MindRoom Chat sends one-click bug reports to the administrators its homeserver's well-known lists."""
    values = "matrix:\n  homeserverUrl: https://chat.example.com\n"
    if rtc:
        values += _MATRIX_RTC
    if admins:
        values += f"bugReports:\n  admins: {json.dumps(_BUG_REPORT_ADMINS)}\n"

    well_known = _matrix_client_well_known(tmp_path, values)

    if not (rtc or admins):
        assert well_known is None
        return
    assert well_known is not None
    assert well_known["m.homeserver"] == {"base_url": "https://chat.example.com"}
    assert ("org.matrix.msc4143.rtc_foci" in well_known) is rtc
    assert well_known.get("io.mindroom.bug_reports") == ({"admins": _BUG_REPORT_ADMINS} if admins else None)


def test_client_chart_keeps_matrix_rtc_discovery_unchanged_without_bug_report_admins(tmp_path: Path) -> None:
    """Deployments that publish only MatrixRTC keep the same nginx config, so its checksum triggers no rollout."""
    docs = _render_chart(
        Path("cluster/k8s/client"),
        release_name="mindroom-client",
        values_files=_values_files(tmp_path, "matrix:\n  homeserverUrl: https://chat.example.com\n" + _MATRIX_RTC),
    )
    nginx_conf = next(doc["data"]["default.conf"] for doc in docs if "default.conf" in doc.get("data", {}))

    assert (
        "\n\n  # MatrixRTC backend discovery for Matrix voice and video calls.\n"
        "  location = /.well-known/matrix/client {\n"
    ) in nginx_conf
    assert "Bug-report" not in nginx_conf


@pytest.mark.parametrize(
    ("values", "error"),
    [
        ('bugReports:\n  admins: "@admin:chat.example.com"\n', "bugReports.admins must be a list"),
        ('bugReports:\n  admins: ["admin"]\n', "bugReports.admins entries must be Matrix user IDs"),
        ('bugReports:\n  admins: ["@admin chat:example.com"]\n', "bugReports.admins entries must be Matrix user IDs"),
        # A quote would end the nginx return body, nginx would unescape the JSON's backslash escapes,
        # and a $ would expand as an nginx variable.
        ('bugReports:\n  admins: ["@a\'b:chat.example.com"]\n', "bugReports.admins entries must be Matrix user IDs"),
        ("bugReports:\n  admins: ['@a\"b:chat.example.com']\n", "bugReports.admins entries must be Matrix user IDs"),
        ("bugReports:\n  admins: ['@a\\b:chat.example.com']\n", "bugReports.admins entries must be Matrix user IDs"),
        ('bugReports:\n  admins: ["@admin:chat$host"]\n', "bugReports.admins entries must be Matrix user IDs"),
        (
            'matrix:\n  homeserverUrl: ""\n  defaultServerName: chat.example.com\nbugReports:\n  admins: ["@admin:chat.example.com"]\n',
            "matrix.homeserverUrl is required when bugReports.admins is set",
        ),
        (
            'nginx:\n  existingConfigMap: client-nginx\nbugReports:\n  admins: ["@admin:chat.example.com"]\n',
            "bugReports.admins requires the chart-managed nginx config",
        ),
    ],
    ids=[
        "not-a-list",
        "no-sigil",
        "whitespace",
        "quote",
        "double-quote",
        "backslash",
        "dollar",
        "no-homeserver-url",
        "existing-nginx",
    ],
)
def test_client_chart_rejects_bug_report_admins_it_cannot_publish(tmp_path: Path, values: str, error: str) -> None:
    """Administrators that cannot be published intact fail the render instead of silently disabling reports."""
    completed = _run_helm_template(
        Path("cluster/k8s/client"),
        release_name="mindroom-client",
        values_files=_values_files(tmp_path, values),
    )

    assert completed.returncode != 0
    assert error in completed.stderr
