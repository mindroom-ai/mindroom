"""End-to-end Desktop CLI pairing and bridge commands through a Cloudflare Access style proxy.

The homeserver below behaves like a Matrix server behind Cloudflare Access:
every request except ``/_matrix/client/versions`` must carry the user's Access
JWT in ``cf-access-token`` or it receives the Access login redirect.

The desktop side runs the real ``mindroom desktop`` CLI in a subprocess, as on a
user's computer: ``cloudflared`` is a fake executable on ``PATH`` and the Matrix
SSO browser is a fake ``$BROWSER``.  The cloud side is a real Olm controller
device running the production pairing receiver and desktop response router.
Keeping the two nio stores in separate processes also mirrors production; nio
binds its store models per call, so two stores must not run on two threads.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode
from uuid import uuid4

import nio
import pytest
from aiohttp import web
from nio.store import SqliteMemoryStore

from mindroom.desktop.client import DesktopResponseRouter
from mindroom.desktop.pairing import confirm_desktop_pairing, create_desktop_pairing
from mindroom.desktop.pairing_receiver import DesktopPairingReceiver
from mindroom.desktop.protocol import DesktopCommand, DesktopResponse, desktop_pairing_verification
from mindroom.desktop.session import load_desktop_session
from mindroom.matrix.client_session import MindRoomAsyncClient, matrix_client_config
from mindroom.matrix.olm_to_device import PinnedMatrixDevice
from tests.conftest import test_runtime_paths as make_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths

pytestmark = pytest.mark.skipif(os.name == "nt", reason="fake cloudflared and browser are POSIX scripts")

REQUESTER = "@alice:example.org"
DESKTOP_DEVICE = "ALICEDESKTOP"
CONTROLLER = "@computer:example.org"
CONTROLLER_DEVICE = "CLOUD"
AGENT = "computer"
LOGIN_TOKEN = "sso-login-token"  # noqa: S105 - test-only value
ACCESS_TOKEN_HEADER = "cf-access-token"  # noqa: S105 - header name, not a credential
PUBLIC_PATHS = frozenset({"/_matrix/client/versions", "/cdn-cgi/access/login"})


def _access_jwt() -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 3600}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


@dataclass
class _AccessHomeserver:
    """Minimal Matrix homeserver with Cloudflare Access enforcement and to-device routing."""

    access_token: str
    requests: list[tuple[str, str]] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    unhandled: list[tuple[str, str]] = field(default_factory=list)
    device_keys: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    one_time_keys: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    inboxes: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = field(default_factory=dict)
    sequence: int = 0
    tokens: dict[str, tuple[str, str]] = field(default_factory=dict)
    inbox_changed: asyncio.Event = field(default_factory=asyncio.Event)

    @web.middleware
    async def access(self, request: web.Request, handler: Any) -> web.StreamResponse:  # noqa: ANN401
        if request.path not in PUBLIC_PATHS and request.headers.get(ACCESS_TOKEN_HEADER) != self.access_token:
            self.rejected.append((request.method, request.path))
            access_login = "/cdn-cgi/access/login"
            raise web.HTTPFound(access_login)
        self.requests.append((request.method, request.path))
        return await handler(request)

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self.access])
        client = "/_matrix/client/v3"
        app.router.add_get("/_matrix/client/versions", self.versions)
        app.router.add_get("/cdn-cgi/access/login", self.access_login_page)
        app.router.add_get(f"{client}/login", self.login_flows)
        app.router.add_get(f"{client}/login/sso/redirect", self.sso_redirect)
        app.router.add_post(f"{client}/login", self.login)
        app.router.add_post(f"{client}/keys/upload", self.keys_upload)
        app.router.add_post(f"{client}/keys/query", self.keys_query)
        app.router.add_post(f"{client}/keys/claim", self.keys_claim)
        app.router.add_put(f"{client}/sendToDevice/{{event_type}}/{{txn_id}}", self.send_to_device)
        app.router.add_get(f"{client}/sync", self.sync)
        app.router.add_route("*", "/{path:.*}", self.unrecognized)
        return app

    def _device(self, request: web.Request) -> tuple[str, str]:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in self.tokens:
            raise web.HTTPUnauthorized(
                text=json.dumps({"errcode": "M_UNKNOWN_TOKEN", "error": "Unknown token"}),
                content_type="application/json",
            )
        return self.tokens[token]

    async def versions(self, _request: web.Request) -> web.Response:
        return web.json_response({"versions": ["v1.11"]})

    async def access_login_page(self, _request: web.Request) -> web.Response:
        return web.Response(text="<html>Cloudflare Access sign in</html>", content_type="text/html")

    async def login_flows(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "flows": [
                    {"type": "m.login.token"},
                    {"type": "m.login.sso", "identity_providers": [{"id": "idp", "name": "IdP"}]},
                ],
            },
        )

    async def sso_redirect(self, request: web.Request) -> web.Response:
        callback = f"{request.query['redirectUrl']}?{urlencode({'loginToken': LOGIN_TOKEN})}"
        raise web.HTTPFound(callback)

    async def login(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("type") != "m.login.token" or body.get("token") != LOGIN_TOKEN:
            return web.json_response({"errcode": "M_FORBIDDEN", "error": "Invalid login"}, status=403)
        self.tokens["desktop-access-token"] = (REQUESTER, DESKTOP_DEVICE)
        return web.json_response(
            {"user_id": REQUESTER, "access_token": "desktop-access-token", "device_id": DESKTOP_DEVICE},
        )

    async def keys_upload(self, request: web.Request) -> web.Response:
        user_id, device_id = self._device(request)
        body = await request.json()
        if "device_keys" in body:
            self.device_keys.setdefault(user_id, {})[device_id] = body["device_keys"]
        self.one_time_keys.setdefault((user_id, device_id), {}).update(body.get("one_time_keys", {}))
        count = len(self.one_time_keys[(user_id, device_id)])
        return web.json_response({"one_time_key_counts": {"signed_curve25519": count}})

    async def keys_query(self, request: web.Request) -> web.Response:
        self._device(request)
        body = await request.json()
        device_keys = {user: self.device_keys[user] for user in body["device_keys"] if user in self.device_keys}
        failures = {user.partition(":")[2]: {} for user in body["device_keys"] if user not in self.device_keys}
        # Tuwunel omits device_keys when every queried server failed.
        response: dict[str, Any] = {"failures": failures}
        if device_keys:
            response["device_keys"] = device_keys
        return web.json_response(response)

    async def keys_claim(self, request: web.Request) -> web.Response:
        self._device(request)
        body = await request.json()
        claimed: dict[str, dict[str, dict[str, Any]]] = {}
        for user_id, devices in body["one_time_keys"].items():
            for device_id in devices:
                available = self.one_time_keys.get((user_id, device_id), {})
                if available:
                    key_id = next(iter(available))
                    claimed.setdefault(user_id, {})[device_id] = {key_id: available.pop(key_id)}
        return web.json_response({"one_time_keys": claimed, "failures": {}})

    async def send_to_device(self, request: web.Request) -> web.Response:
        sender, _device_id = self._device(request)
        body = await request.json()
        for user_id, devices in body["messages"].items():
            for device_id, content in devices.items():
                self.sequence += 1
                self.inboxes.setdefault((user_id, device_id), []).append(
                    (self.sequence, {"type": request.match_info["event_type"], "sender": sender, "content": content}),
                )
        self.inbox_changed.set()
        return web.json_response({})

    async def sync(self, request: web.Request) -> web.Response:
        identity = self._device(request)
        # Like a real homeserver, keep to-device messages until the next since token acknowledges them.
        since = int(request.query.get("since", "s0").removeprefix("s") or 0)
        inbox = self.inboxes.setdefault(identity, [])
        inbox[:] = [(sequence, event) for sequence, event in inbox if sequence > since]
        deadline = time.monotonic() + min(int(request.query.get("timeout", "0")) / 1000, 0.5)
        while not inbox and time.monotonic() < deadline:
            self.inbox_changed.clear()
            try:
                await asyncio.wait_for(self.inbox_changed.wait(), deadline - time.monotonic())
            except TimeoutError:
                break
        next_batch = max((sequence for sequence, _event in inbox), default=since)
        return web.json_response(
            {
                "next_batch": f"s{next_batch}",
                "to_device": {"events": [event for _sequence, event in inbox]},
            },
        )

    async def unrecognized(self, request: web.Request) -> web.Response:
        self.unhandled.append((request.method, request.path))
        return web.json_response({"errcode": "M_UNRECOGNIZED", "error": "Unrecognized request"}, status=404)


async def _serve_controller(
    homeserver: _AccessHomeserver,
    controller: MindRoomAsyncClient,
    stop: asyncio.Event,
) -> None:
    """Deliver controller to-device mail the way the cloud agent's sync loop would."""
    identity = (CONTROLLER, CONTROLLER_DEVICE)
    batch = 0
    while not stop.is_set():
        events = [event for _sequence, event in homeserver.inboxes.pop(identity, [])]
        if not events:
            await asyncio.sleep(0.02)
            continue
        batch += 1
        # nio ignores a sync response that repeats the previous next_batch token.
        await controller.receive_response(
            nio.SyncResponse.from_dict({"next_batch": f"controller-{batch}", "to_device": {"events": events}}),
        )
        if controller.olm is not None and controller.olm.users_for_key_query:
            await controller.keys_query()


@dataclass
class _CloudSide:
    url: str
    homeserver: _AccessHomeserver
    runtime_paths: RuntimePaths
    controller: MindRoomAsyncClient
    run: Callable[[Coroutine[Any, Any, Any]], Any]

    @property
    def controller_ed25519(self) -> str:
        assert self.controller.olm is not None
        return self.controller.olm.account.identity_keys["ed25519"]


@contextmanager
def _cloud_side(tmp_path: Path, access_token: str) -> Iterator[_CloudSide]:
    """Run the proxy, homeserver, and a real controller device on one background loop."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    def run(coroutine: Any) -> Any:  # noqa: ANN401
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result(timeout=30)

    homeserver = _AccessHomeserver(access_token)
    runtime_paths = make_runtime_paths(tmp_path / "cloud")
    stop = asyncio.Event()

    async def start() -> tuple[web.AppRunner, str, MindRoomAsyncClient, asyncio.Task[None]]:
        homeserver.inbox_changed = asyncio.Event()
        app_runner = web.AppRunner(homeserver.app())
        await app_runner.setup()
        site = web.TCPSite(app_runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        homeserver.tokens["controller-access-token"] = (CONTROLLER, CONTROLLER_DEVICE)
        # The cloud agent reaches Matrix inside the cluster; the header only satisfies this proxy.
        controller = MindRoomAsyncClient(
            url,
            CONTROLLER,
            CONTROLLER_DEVICE,
            config=replace(
                matrix_client_config(http_headers={ACCESS_TOKEN_HEADER: access_token}),
                store=SqliteMemoryStore,
            ),
        )
        controller.restore_login(CONTROLLER, CONTROLLER_DEVICE, "controller-access-token")
        await controller.keys_upload()
        receiver = DesktopPairingReceiver(client=controller, agent_name=AGENT, runtime_paths=runtime_paths)
        controller.add_to_device_callback(receiver.on_event, nio.AuthenticatedToDeviceEvent)  # type: ignore[arg-type]
        task = asyncio.create_task(_serve_controller(homeserver, controller, stop))
        return app_runner, url, controller, task

    app_runner, url, controller, task = run(start())
    try:
        yield _CloudSide(url, homeserver, runtime_paths, controller, run)
    finally:

        async def shutdown() -> None:
            stop.set()
            try:
                await task
            finally:
                await controller.close()
                await app_runner.cleanup()

        try:
            run(shutdown())
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=10)
            loop.close()


_FAKE_CLOUDFLARED = """#!{python} -S
import json, os, pathlib, sys
state = pathlib.Path(os.environ["FAKE_CLOUDFLARED_STATE"])
args = sys.argv[1:]
with (state / "calls.jsonl").open("a", encoding="utf-8") as calls:
    calls.write(json.dumps(args) + "\\n")
if args[:2] == ["access", "token"]:
    if (state / "logged-in").exists():
        print(os.environ["FAKE_ACCESS_TOKEN"])
        sys.exit(0)
    sys.exit(1)
if args[:2] == ["access", "login"]:
    (state / "logged-in").touch()
    sys.exit(0)
sys.exit(2)
"""

# webbrowser waits for $BROWSER to exit, so the fake browser detaches before it follows the SSO redirect.
_FAKE_BROWSER = """#!{python} -S
import os, subprocess, sys, urllib.request
if len(sys.argv) == 2:
    subprocess.Popen([sys.executable, "-S", __file__, sys.argv[1], "--visit"], start_new_session=True)
    sys.exit(0)
request = urllib.request.Request(sys.argv[1], headers={{"cf-access-token": os.environ["FAKE_ACCESS_TOKEN"]}})
urllib.request.urlopen(request, timeout=30).read()
"""

# Replaces only the operating-system provider; the bridge, transport, and Matrix client stay real.
_STATUS_ONLY_SITECUSTOMIZE = """
import os

if os.environ.get("MINDROOM_TEST_STATUS_ONLY_DESKTOP") == "1":
    import mindroom.cli.desktop as desktop_cli
    import mindroom.desktop.provider as provider

    class StatusOnlyProvider:
        def __init__(self, **_kwargs):
            pass

        def status(self):
            return {"screen": {"width": 1440, "height": 900}}

        def check_emergency_stop(self):
            return None

    provider.PyAutoGuiDesktopProvider = StatusOnlyProvider
    desktop_cli._ensure_desktop_dependencies = lambda _runtime_paths: None
    desktop_cli._request_required_desktop_permissions = lambda: None
"""


@dataclass
class _LocalComputer:
    """The user's computer: isolated home, fake cloudflared and browser, and a private storage path."""

    root: Path
    access_token: str

    def __post_init__(self) -> None:
        (self.root / "home").mkdir(parents=True)
        (self.root / "cloudflared-state").mkdir()
        (self.root / "bin").mkdir()
        (self.root / "site").mkdir()
        self._script("bin/cloudflared", _FAKE_CLOUDFLARED)
        self._script("browser", _FAKE_BROWSER)
        (self.root / "site" / "sitecustomize.py").write_text(_STATUS_ONLY_SITECUSTOMIZE, encoding="utf-8")

    def _script(self, name: str, template: str) -> None:
        path = self.root / name
        path.write_text(template.format(python=sys.executable), encoding="utf-8")
        path.chmod(0o755)

    @property
    def storage_path(self) -> Path:
        return self.root / "desktop-state"

    @property
    def cloudflared_calls(self) -> list[list[str]]:
        calls = (self.root / "cloudflared-state" / "calls.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in calls.splitlines()]

    def env(self, *, status_only_provider: bool = False) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"MINDROOM_CONFIG_PATH", "MINDROOM_STORAGE_PATH", "PYTHONPATH"}
        }
        env.update(
            {
                "HOME": str(self.root / "home"),
                "PATH": f"{self.root / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
                "BROWSER": str(self.root / "browser"),
                "PYTHONPATH": str(self.root / "site"),
                "FAKE_CLOUDFLARED_STATE": str(self.root / "cloudflared-state"),
                "FAKE_ACCESS_TOKEN": self.access_token,
                "MINDROOM_NO_AUTO_INSTALL_TOOLS": "1",
                "COLUMNS": "400",
                "NO_COLOR": "1",
            },
        )
        if status_only_provider:
            env["MINDROOM_TEST_STATUS_ONLY_DESKTOP"] = "1"
        return env

    def command(self, *args: str) -> list[str]:
        return [sys.executable, "-c", "from mindroom.cli.main import main; main()", "desktop", *args]

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.command(*args),
            capture_output=True,
            check=False,
            env=self.env(),
            text=True,
            timeout=120,
        )


def _setup_args(cloud: _CloudSide, local: _LocalComputer, *, code: str, controller_user_id: str) -> list[str]:
    """Return the command printed by ``!desktop setup`` plus the documented separate storage path."""
    return [
        "setup",
        "--user-id",
        REQUESTER,
        "--homeserver",
        cloud.url,
        "--code",
        code,
        "--controller-user-id",
        controller_user_id,
        "--controller-device-id",
        CONTROLLER_DEVICE,
        "--controller-ed25519",
        cloud.controller_ed25519,
        "--cloudflare-access",
        "--storage-path",
        str(local.storage_path),
    ]


@dataclass
class _PairedDesktop:
    token: str
    ed25519: str
    output: str


def _pair_through_setup(cloud: _CloudSide, local: _LocalComputer) -> _PairedDesktop:
    pairing = create_desktop_pairing(cloud.runtime_paths, requester_id=REQUESTER, agent_name=AGENT)
    result = local.run(*_setup_args(cloud, local, code=pairing.token, controller_user_id=CONTROLLER))
    assert result.returncode == 0, result.stdout + result.stderr
    desktop_ed25519 = cloud.homeserver.device_keys[REQUESTER][DESKTOP_DEVICE]["keys"][f"ed25519:{DESKTOP_DEVICE}"]
    return _PairedDesktop(pairing.token, desktop_ed25519, result.stdout)


def test_desktop_setup_pairs_through_cloudflare_access(tmp_path: Path) -> None:
    """Setup logs in through Access and SSO, pairs, and receives the controller acknowledgement."""
    access_token = _access_jwt()
    local = _LocalComputer(tmp_path / "local", access_token)

    with _cloud_side(tmp_path, access_token) as cloud:
        paired = _pair_through_setup(cloud, local)
        homeserver = cloud.homeserver
        verification = desktop_pairing_verification(paired.token, paired.ed25519)
        confirmed = confirm_desktop_pairing(
            cloud.runtime_paths,
            token=paired.token,
            requester_id=REQUESTER,
            agent_name=AGENT,
            verification=verification,
        )

    assert f"!desktop confirm {paired.token} {verification}" in paired.output
    assert homeserver.rejected == []
    assert homeserver.unhandled == []
    assert (confirmed.device_user_id, confirmed.device_id, confirmed.device_ed25519) == (
        REQUESTER,
        DESKTOP_DEVICE,
        paired.ed25519,
    )
    session_path = local.storage_path / "desktop_bridge" / "matrix_session.json"
    saved = load_desktop_session(session_path)
    assert (saved.homeserver, saved.user_id, saved.device_id, saved.cloudflare_access) == (
        cloud.url,
        REQUESTER,
        DESKTOP_DEVICE,
        True,
    )
    assert access_token not in session_path.read_text(encoding="utf-8")
    exercised = {path.removeprefix("/_matrix/client/v3").split("/")[1] for _method, path in homeserver.requests}
    assert {"login", "keys", "sendToDevice", "sync"} <= exercised
    calls = local.cloudflared_calls
    assert calls[:3] == [
        ["access", "token", f"-app={cloud.url}"],
        ["access", "login", "--quiet", cloud.url],
        ["access", "token", f"-app={cloud.url}"],
    ]
    assert {call[-1] for call in calls} == {f"-app={cloud.url}", cloud.url}
    assert sum(call[:2] == ["access", "login"] for call in calls) == 1


def test_desktop_setup_reports_unreachable_controller(tmp_path: Path) -> None:
    """A controller the homeserver cannot resolve fails with a pairing error, not a traceback."""
    access_token = _access_jwt()
    local = _LocalComputer(tmp_path / "local", access_token)

    with _cloud_side(tmp_path, access_token) as cloud:
        pairing = create_desktop_pairing(cloud.runtime_paths, requester_id=REQUESTER, agent_name=AGENT)
        # Another deployment's controller: this homeserver reports only a federation failure.
        result = local.run(
            *_setup_args(cloud, local, code=pairing.token, controller_user_id="@computer:other.example.org"),
        )

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "Traceback" not in output
    assert "Desktop pairing failed:" in output
    assert "@computer:other.example.org" in output


@contextmanager
def _running_bridge(cloud: _CloudSide, local: _LocalComputer) -> Iterator[list[str]]:
    """Run ``mindroom desktop run`` observe-only, then stop it with Ctrl-C."""
    process = subprocess.Popen(
        local.command(
            "run",
            "--controller-user-id",
            CONTROLLER,
            "--controller-device-id",
            CONTROLLER_DEVICE,
            "--controller-ed25519",
            cloud.controller_ed25519,
            "--allow-requester",
            REQUESTER,
            "--allow-agent",
            AGENT,
            "--allow-app",
            "com.example.Editor",
            "--storage-path",
            str(local.storage_path),
        ),
        env=local.env(status_only_provider=True),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert process.stdout is not None
    lines: list[str] = []
    online = threading.Event()

    def read_output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.append(line)
            if "Desktop bridge online" in line:
                online.set()

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    try:
        assert online.wait(timeout=60), "".join(lines)
        yield lines
    finally:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            reader.join(timeout=10)


def test_desktop_bridge_answers_controller_through_cloudflare_access(tmp_path: Path) -> None:
    """A paired observe-only bridge syncs, decrypts, and answers a controller command behind Access."""
    access_token = _access_jwt()
    local = _LocalComputer(tmp_path / "local", access_token)

    with _cloud_side(tmp_path, access_token) as cloud:
        paired = _pair_through_setup(cloud, local)
        confirm_desktop_pairing(
            cloud.runtime_paths,
            token=paired.token,
            requester_id=REQUESTER,
            agent_name=AGENT,
            verification=desktop_pairing_verification(paired.token, paired.ed25519),
        )
        now_ms = int(time.time() * 1000)
        command = DesktopCommand(
            request_id=str(uuid4()),
            session_id=str(uuid4()),
            sequence=1,
            issued_at_ms=now_ms,
            expires_at_ms=now_ms + 60_000,
            action="status",
            requester_id=REQUESTER,
            agent_name=AGENT,
        )

        async def request_status() -> DesktopResponse:
            router = DesktopResponseRouter(cloud.controller)
            target = PinnedMatrixDevice(REQUESTER, DESKTOP_DEVICE, paired.ed25519)
            # Below the 30 s cloud-loop wait so the router's own timeout message is reported.
            return await router.request(target, command, timeout_seconds=20)

        with _running_bridge(cloud, local) as output:
            response = cloud.run(request_status())
        homeserver = cloud.homeserver

    assert "Desktop bridge stopped" in "".join(output), "".join(output)
    assert homeserver.rejected == []
    assert homeserver.unhandled == []
    assert response.ok, response.error
    assert response.request_id == command.request_id
    assert response.result["screen"] == {"width": 1440, "height": 900}
