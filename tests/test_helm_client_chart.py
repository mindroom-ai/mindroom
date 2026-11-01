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

from tests.test_helm_instance_worker_isolation import _render_chart, _resource, _run_helm_template, _values_files

_NGINX_IMAGE = "mirror.gcr.io/library/nginx:alpine"
_IMMUTABLE = "public, max-age=31536000, immutable"


def _client_nginx_conf(*set_args: str) -> str:
    docs = _render_chart(Path("cluster/k8s/client"), *set_args, release_name="mindroom-client")
    return next(doc["data"]["default.conf"] for doc in docs if "default.conf" in doc.get("data", {}))


def _serve(
    nginx_conf: str,
    files: dict[str, str],
    paths: list[str],
    headers: tuple[str, ...],
) -> dict[str, tuple[str, ...]]:
    """Serve nginx_conf in nginx with files planted at their absolute paths.

    Returns each path's status code, the requested response headers, and the body.
    """
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker is required to serve the rendered nginx config")
    if subprocess.run([docker, "info"], check=False, capture_output=True).returncode != 0:
        pytest.skip("Docker daemon is unavailable to serve the rendered nginx config")
    write_out = "\\t".join(["%{http_code}", *(f"%header{{{header}}}" for header in headers)])
    script = "\n".join(
        [
            "set -eu",
            "cat > /etc/nginx/conf.d/default.conf",
            *(
                f"mkdir -p {shlex.quote(str(PurePosixPath(path).parent))} && printf %s {shlex.quote(body)} > {path}"
                for path, body in files.items()
            ),
            # The image logs requests to stdout, which carries the responses.
            "nginx >&2",
            *(
                f"curl -s --path-as-is -o /tmp/body -w '{write_out}\\t' "
                f"{shlex.quote(f'http://127.0.0.1:8080{path}')} && tr -d '\\r\\n' < /tmp/body && echo"
                for path in paths
            ),
        ],
    )

    completed = subprocess.run(
        [docker, "run", "--rm", "-i", _NGINX_IMAGE, "sh", "-c", script],
        input=nginx_conf,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    return {
        path: tuple(line.split("\t", len(headers) + 1))
        for path, line in zip(paths, completed.stdout.splitlines(), strict=True)
    }


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
    files = {
        "/usr/share/nginx/html/assets/app-1a2b.js": "app bundle",
        "/usr/share/nginx/html/public/locales/en.json": "translations",
        "/usr/share/nginx/html/public/element-call/index.html": "call page",
        "/usr/share/nginx/html/public/element-call/assets/call-3c4d.js": "call bundle",
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

    responses = _serve(
        _client_nginx_conf(f"basePath={base_path}", "nginx.ipv6=false"),
        files,
        list(expected),
        ("cache-control",),
    )

    actual = {
        path: (status, cache_control, body if status == "200" else None)
        for path, (status, cache_control, body) in responses.items()
    }
    assert actual == expected


@pytest.mark.parametrize("base_path", ["/", "/chat"])
def test_client_chart_serves_pairing_notices_and_app_links_like_the_image(base_path: str) -> None:
    """The chart replaces the image's nginx config, so it must keep the image's routes for these client features.

    The pairing page approves a sign-in, so no origin may frame it, unlike the rest of the app.
    Notices come from the image's notices directory, which operators replace, not from the build output.
    iOS reads app links from the origin root whatever the base path.
    """
    prefix = base_path.rstrip("/")
    app_links = '{"applinks":{}}'
    files = {
        "/usr/share/nginx/html/index.html": "app shell",
        "/usr/share/nginx/html/notices.json": "[]",
        "/usr/share/nginx/html/.well-known/apple-app-site-association": app_links,
        "/opt/mindroom/notices/notices.json": '[{"id":"maintenance"}]',
    }
    no_store = "no-store, no-cache, must-revalidate, max-age=0"
    pairing = ("200", "text/html", no_store, "frame-ancestors 'none'", "DENY", "app shell")
    expected = {
        f"{prefix}/connect": pairing,
        f"{prefix}/connect/": pairing,
        f"{prefix}/connections": ("200", "text/html", no_store, "frame-ancestors 'self'", "SAMEORIGIN", "app shell"),
        f"{prefix}/notices.json": ("200", "application/json", "no-store, max-age=0", "", "", '[{"id":"maintenance"}]'),
        "/.well-known/apple-app-site-association": ("200", "application/json", "", "", "", app_links),
    }

    responses = _serve(
        _client_nginx_conf(f"basePath={base_path}", "nginx.ipv6=false"),
        files,
        list(expected),
        ("content-type", "cache-control", "content-security-policy", "x-frame-options"),
    )

    assert responses == expected


@pytest.mark.parametrize("base_path", ["/", "/chat"])
def test_client_chart_answers_404_below_the_files_a_gateway_makes_public(base_path: str) -> None:
    """A gateway rule that makes a file public also covers every path below it.

    Browsers fetch the runtime config, the recovery script, and the service workers without a gateway session,
    so an operator makes them public; the app shell and build files must not answer below them.
    Some gateways treat a backslash as a slash, and some match the path before resolving its dot segments.
    """
    prefix = base_path.rstrip("/")
    files = {
        "/usr/share/nginx/html/index.html": "app shell",
        "/usr/share/nginx/html/assets/app-1a2b.js": "app bundle",
        "/usr/share/nginx/html/authentication-recovery.js": "recovery script",
        "/usr/share/nginx/html/sw.js": "service worker",
        "/usr/share/nginx/html/root-cleanup-sw.js": "cleanup worker",
    }
    public_files = {
        # runtime-config.js is a list of statements; the first one sets the base path.
        "/runtime-config.js": f'window.__APP_BASE_PATH__ = "{base_path}"',
        "/authentication-recovery.js": "recovery script",
        f"{prefix}/sw.js": "service worker",
    }
    set_args = [f"basePath={base_path}", "nginx.ipv6=false", "authenticationRecovery.enabled=true"]
    if prefix:
        public_files["/sw.js"] = "cleanup worker"
        set_args.append("rootServiceWorkerCleanup.enabled=true")
    expected = {
        **{f"{path}{query}": ("200", body) for path, body in public_files.items() for query in ["", "?v=1"]},
        **{
            f"{path}{below}": ("404", None)
            for path in public_files
            for below in [
                "/",
                "/x",
                "//x",
                "%2Fx",
                "%5Cx",
                "/assets/app-1a2b.js",
                "/../assets/app-1a2b.js",
                "/%2e%2e/assets/app-1a2b.js",
                "%2F%2E%2E%2Fassets/app-1a2b.js",
            ]
        },
        **{f"/{path}/x": ("404", None) for path in public_files},
        f"{prefix}/rooms/abc": ("200", "app shell"),
        f"{prefix}/assets/app-1a2b.js": ("200", "app bundle"),
    }

    responses = _serve(_client_nginx_conf(*set_args), files, list(expected), ())

    actual = {
        path: (status, body.split(";", 1)[0] if status == "200" else None) for path, (status, body) in responses.items()
    }
    assert actual == expected


def _client_pod(tmp_path: Path, values: str) -> dict[str, Any]:
    docs = _render_chart(
        Path("cluster/k8s/client"),
        release_name="mindroom-client",
        values_files=_values_files(tmp_path, values),
    )
    return _resource(docs, "Deployment", "mindroom-client")["spec"]["template"]["spec"]


def test_client_chart_mounts_operator_notices_so_edits_reach_running_pods(tmp_path: Path) -> None:
    """A subPath mount would pin the file the pod started with, so notice edits would wait for a restart.

    The ConfigMap is optional so the client still starts, without notices, before an operator creates it.
    """
    pod = _client_pod(tmp_path, "notices:\n  existingConfigMap: client-notices\n")

    assert {"name": "notices", "configMap": {"name": "client-notices", "optional": True}} in pod["volumes"]
    assert {"name": "notices", "mountPath": "/opt/mindroom/notices", "readOnly": True} in pod["containers"][0][
        "volumeMounts"
    ]

    default_pod = _client_pod(tmp_path, "{}\n")
    assert "notices" not in {volume["name"] for volume in default_pod["volumes"]}


def _client_main_nginx_conf(tmp_path: Path, values: str) -> str | None:
    docs = _render_chart(
        Path("cluster/k8s/client"),
        release_name="mindroom-client",
        values_files=_values_files(tmp_path, values),
    )
    pod = _resource(docs, "Deployment", "mindroom-client")["spec"]["template"]["spec"]
    mounts = {mount["mountPath"]: mount for mount in pod["containers"][0]["volumeMounts"]}
    if "/etc/nginx/nginx.conf" not in mounts:
        return None
    assert mounts["/etc/nginx/nginx.conf"] == {
        "name": "nginx-config",
        "mountPath": "/etc/nginx/nginx.conf",
        "subPath": "nginx.conf",
        "readOnly": True,
    }
    return next(doc["data"]["nginx.conf"] for doc in docs if "nginx.conf" in doc.get("data", {}))


@pytest.mark.parametrize(
    ("values", "user_directive"),
    [
        ("{}\n", False),
        ("podSecurityContext:\n  runAsNonRoot: true\n", False),
        ("securityContext:\n  runAsUser: 0\n  runAsNonRoot: false\n", True),
        ("podSecurityContext: null\n", True),
    ],
    ids=["default", "run-as-non-root", "container-root", "image-user"],
)
def test_client_chart_sets_the_nginx_user_only_when_nginx_may_run_as_root(
    tmp_path: Path,
    values: str,
    user_directive: bool,
) -> None:
    """An unprivileged master cannot switch workers to another user and warns about the directive at every start."""
    main_conf = _client_main_nginx_conf(tmp_path, values)

    assert main_conf is not None
    assert ("user nginx;" in main_conf.splitlines()) is user_directive
    assert "error_log /var/log/nginx/error.log warn;" in main_conf.splitlines()


def test_client_chart_keeps_the_image_main_nginx_config_with_an_external_server_config(tmp_path: Path) -> None:
    """An external ConfigMap need not carry the chart's main config, so the pod does not mount it from there."""
    assert _client_main_nginx_conf(tmp_path, "nginx:\n  existingConfigMap: custom-nginx\n") is None


@pytest.mark.parametrize(
    "values",
    [
        "nginx:\n  key: nginx.conf\n",
        "basePath: /chat\nrootServiceWorkerCleanup:\n  enabled: true\nnginx:\n  cleanupWorkerKey: nginx.conf\n",
    ],
    ids=["server-config", "cleanup-worker"],
)
def test_client_chart_rejects_nginx_keys_that_would_replace_the_main_config(tmp_path: Path, values: str) -> None:
    """A second nginx.conf key would silently overwrite the main config in the ConfigMap."""
    completed = _run_helm_template(
        Path("cluster/k8s/client"),
        release_name="mindroom-client",
        values_files=_values_files(tmp_path, values),
    )

    assert completed.returncode != 0
    assert "nginx.key and nginx.cleanupWorkerKey cannot be nginx.conf" in completed.stderr


def test_client_chart_nginx_logs_only_requests_when_it_starts_serves_and_stops(tmp_path: Path) -> None:
    """Log collectors read every stderr line as an error, so routine warnings and notices there bury real errors."""
    docker = shutil.which("docker")
    if docker is None or subprocess.run([docker, "info"], check=False, capture_output=True).returncode:
        pytest.skip("Docker is required to run the rendered nginx config")
    main_conf = _client_main_nginx_conf(tmp_path, "{}\n")
    assert main_conf is not None
    pod = _client_pod(tmp_path, "{}\n")
    uid, gid = pod["securityContext"]["runAsUser"], pod["securityContext"]["runAsGroup"]
    files = {
        "/etc/nginx/nginx.conf": main_conf,
        "/etc/nginx/conf.d/default.conf": _client_nginx_conf("nginx.ipv6=false"),
        "/usr/share/nginx/html/config.json": "{}",
    }
    mounts = []
    for index, (path, content) in enumerate(files.items()):
        source = tmp_path / f"mount-{index}"
        source.write_text(content)
        source.chmod(0o644)
        mounts += ["--volume", f"{source}:{path}:ro"]
    script = "\n".join(
        [
            "set -eu",
            f"{shlex.join(pod['containers'][0]['command'])} &",
            "for _ in $(seq 50); do curl -sf -o /dev/null http://127.0.0.1:8080/config.json && break; sleep 0.1; done",
            # The image stops nginx with SIGQUIT, a graceful shutdown.
            'kill -QUIT "$!"',
            'wait "$!"',
        ],
    )

    # Like the pod: its user, a read-only root, no capabilities, and emptyDirs that its fsGroup can write.
    completed = subprocess.run(
        [
            docker,
            "run",
            "--rm",
            # Keeps a first pull's progress off stderr.
            "--quiet",
            "--user",
            f"{uid}:{gid}",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--tmpfs",
            f"/run:uid={uid},gid={gid}",
            "--tmpfs",
            f"/var/cache/nginx:uid={uid},gid={gid}",
            *mounts,
            _NGINX_IMAGE,
            "sh",
            "-c",
            script,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert '"GET /config.json HTTP/1.1" 200' in completed.stdout


# Historical uppercase localparts, ports, and IPv6 server names are valid Matrix user IDs the client accepts.
_BUG_REPORT_ADMINS = ["@admin:chat.example.com", "@Ops=Team/1+x:chat.example.com:8448", "@admin:[2001:db8::1]"]
_MATRIX_RTC = """
matrixRTC:
  enabled: true
  livekitServiceUrl: https://chat.example.com/livekit/jwt
"""
_EMOJI_ROOM_ERROR = "serverEmoji.room must be a room alias such as #emoji:example.com or a room ID"


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


@pytest.mark.parametrize("rtc", [False, True], ids=["no-rtc", "rtc"])
@pytest.mark.parametrize(
    "room",
    ["", "#emoji:chat.example.com", "#emoji:chat.example.com:8448", "!Zk3vQ9mT1nXb7YpLhR2sWc5dEoA8jUfGiN4tBqHyV-w"],
    ids=["no-room", "alias", "alias-port", "room-id"],
)
def test_client_chart_publishes_the_server_emoji_room_in_matrix_client_discovery(
    tmp_path: Path,
    *,
    rtc: bool,
    room: str,
) -> None:
    """MindRoom Chat finds the server's custom emoji room through the well-known of its homeserver.

    The room ID has no server part, as room version 12 IDs do.
    """
    values = "matrix:\n  homeserverUrl: https://chat.example.com\n"
    if rtc:
        values += _MATRIX_RTC
    if room:
        values += f"serverEmoji:\n  room: {json.dumps(room)}\n"

    well_known = _matrix_client_well_known(tmp_path, values)

    if not (rtc or room):
        assert well_known is None
        return
    assert well_known is not None
    assert well_known["m.homeserver"] == {"base_url": "https://chat.example.com"}
    assert ("org.matrix.msc4143.rtc_foci" in well_known) is rtc
    assert well_known.get("io.mindroom.emoji") == ({"room": room} if room else None)


def test_client_chart_keeps_matrix_rtc_discovery_unchanged_without_other_discovery_values(tmp_path: Path) -> None:
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
    assert "Server custom emoji" not in nginx_conf


@pytest.mark.parametrize(
    ("values", "error"),
    [
        ('bugReports:\n  admins: "@admin:chat.example.com"\n', "bugReports.admins must be a list"),
        ('bugReports:\n  admins: ["admin"]\n', "bugReports.admins entries must be Matrix user IDs"),
        ('bugReports:\n  admins: ["@admin chat:example.com"]\n', "bugReports.admins entries must be Matrix user IDs"),
        # A C1 control character would otherwise break the rendered ConfigMap YAML.
        ('bugReports:\n  admins: ["@a\\x85b:chat.example.com"]\n', "bugReports.admins entries must be Matrix user IDs"),
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
        ("serverEmoji:\n  room: emoji\n", _EMOJI_ROOM_ERROR),
        ('serverEmoji:\n  room: "#emoji chat:example.com"\n', _EMOJI_ROOM_ERROR),
        ('serverEmoji:\n  room: "#a\\x85b:chat.example.com"\n', _EMOJI_ROOM_ERROR),
        ('serverEmoji:\n  room: "#a\'b:chat.example.com"\n', _EMOJI_ROOM_ERROR),
        ('serverEmoji:\n  room: "#emoji:chat$host"\n', _EMOJI_ROOM_ERROR),
        ('serverEmoji:\n  room: "#emoji:example.com/path"\n', _EMOJI_ROOM_ERROR),
        (
            'matrix:\n  homeserverUrl: ""\n  defaultServerName: chat.example.com\nserverEmoji:\n  room: "#emoji:chat.example.com"\n',
            "matrix.homeserverUrl is required when serverEmoji.room is set",
        ),
        (
            'nginx:\n  existingConfigMap: client-nginx\nserverEmoji:\n  room: "#emoji:chat.example.com"\n',
            "serverEmoji.room requires the chart-managed nginx config",
        ),
    ],
    ids=[
        "not-a-list",
        "no-sigil",
        "whitespace",
        "control",
        "quote",
        "double-quote",
        "backslash",
        "dollar",
        "no-homeserver-url",
        "existing-nginx",
        "emoji-no-sigil",
        "emoji-whitespace",
        "emoji-control",
        "emoji-quote",
        "emoji-dollar",
        "emoji-path",
        "emoji-no-homeserver-url",
        "emoji-existing-nginx",
    ],
)
def test_client_chart_rejects_discovery_values_it_cannot_publish(tmp_path: Path, values: str, error: str) -> None:
    """Values that cannot be published intact fail the render instead of silently disabling the feature."""
    completed = _run_helm_template(
        Path("cluster/k8s/client"),
        release_name="mindroom-client",
        values_files=_values_files(tmp_path, values),
    )

    assert completed.returncode != 0
    assert error in completed.stderr
