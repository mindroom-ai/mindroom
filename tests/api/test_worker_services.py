"""A dedicated worker's runner starts, reports, and stops the services plugins run in it."""

from __future__ import annotations

import asyncio
import gzip
import json
import random
import re
import shutil
import socket
import sys
import threading
from contextlib import asynccontextmanager, nullcontext, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiohttp
import pytest
import uvicorn
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from mindroom import plugin_worker_services
from mindroom.api import config_lifecycle, sandbox_runner_app, worker_preview
from mindroom.api import worker_services as worker_services_module
from mindroom.api.app_state import ensure_app_state
from mindroom.api.sandbox_runner import initialize_sandbox_runner_app
from mindroom.api.worker_services import close_worker_services, worker_services
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.message_target import MessageTarget
from mindroom.plugin_api import dump_worker_target, load_worker_target
from mindroom.plugin_context import build_plugin_context
from mindroom.plugin_http import WorkerRelay, worker_relay
from mindroom.plugin_lifecycle import PluginLifespans
from mindroom.plugin_worker_relay import open_worker_relay
from mindroom.plugin_worker_services import (
    WorkerService,
    WorkerServiceError,
    ensure_worker_service,
    stop_worker_service,
)
from mindroom.previews import listeners
from mindroom.tool_system import sandbox_proxy
from mindroom.tool_system.plugins import PLUGIN_NAME_SCOPE_KEY
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target, subagent_worker_key
from mindroom.workers.compatibility import WORKER_PROTOCOL_VERSION
from mindroom.workers.in_flight import touch_workers_in_flight
from mindroom.workers.models import WorkerHandle, WorkerSpec
from mindroom.workers.placements import DedicatedWorkerPlacement, dedicated_worker_placement
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    publish_test_plugins,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from mindroom.constants import RuntimePaths
    from mindroom.plugin_context import PluginContext
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="Services run in their own process groups."),
    # The runner serves WebSockets with uvicorn's default implementation, the deprecated websockets.legacy one.
    pytest.mark.filterwarnings(r"ignore:websockets\.legacy is deprecated.*:DeprecationWarning:websockets\.legacy"),
    pytest.mark.filterwarnings(
        r"ignore:websockets\.server\.WebSocketServerProtocol is deprecated:DeprecationWarning:uvicorn\.protocols\.websockets\.websockets_impl",
    ),
    pytest.mark.filterwarnings(
        r"ignore:remove second argument of ws_handler:DeprecationWarning:websockets\.legacy\.server",
    ),
]

_TOKEN = "runner-secret"  # noqa: S105 - isolated test credential
_AUTH = {"X-Mindroom-Sandbox-Token": _TOKEN}
# The key of the worker the agent ``coder``'s turns use, which the runner is pinned to.
_WORKER_KEY = "v1:default:shared:coder"
# A service that records its pid, its child's, and the secret it read, and answers "hello" only with that secret.
_HELLO = """
import json, os, subprocess, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

secret = sys.stdin.readline().strip()
# A process the service starts, which a stop asks to end with it, as a TERM signal it takes a while to record.
CHILD = "import signal, sys, time; signal.signal(signal.SIGTERM, lambda *_: (time.sleep(0.3), sys.exit(open('child-term', 'w').close()))); open('child-ready', 'w').close(); time.sleep(600)"
child = subprocess.Popen([sys.executable, "-c", CHILD])
with open("service.json", "w") as record:
    runner_url = os.environ["MINDROOM_WORKER_SERVICE_RUNNER_URL"]
    json.dump({"pid": os.getpid(), "child": child.pid, "secret": secret, "runner_url": runner_url}, record)


class Hello(BaseHTTPRequestHandler):
    def do_GET(self):
        allowed = self.headers.get("X-MindRoom-Service-Secret") == secret
        self.send_response(200 if allowed else 403)
        self.end_headers()
        self.wfile.write(b"hello" if allowed else b"refused")


ThreadingHTTPServer(("127.0.0.1", int(os.environ["MINDROOM_WORKER_SERVICE_PORT"])), Hello).serve_forever()
"""

# A service that, like the child it starts, ignores the TERM signal a stop sends first, and accepts connections.
_STUBBORN = """
import json, os, signal, socket, subprocess, sys

signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
with open("service.json", "w") as record:
    json.dump({"pid": os.getpid(), "child": child.pid}, record)
server = socket.create_server(("127.0.0.1", int(os.environ["MINDROOM_WORKER_SERVICE_PORT"])))
while True:
    server.accept()[0].close()
"""


@dataclass(frozen=True)
class _Runner:
    port: int
    plugin_root: Path
    workspace: Path

    def url(self, name: str = "hello", plugin: str = "demo") -> str:
        return f"http://127.0.0.1:{self.port}/api/sandbox-runner/plugin-services/{plugin}/{name}"

    def record(self) -> dict[str, object]:
        return json.loads((self.workspace / "service.json").read_text())


def _config(plugin_root: Path, paths: RuntimePaths) -> Config:
    """A config loading the plugin at ``plugin_root`` with the agent ``coder``, whose worker is shared."""
    agent = AgentConfig(display_name="Coder", worker_scope="shared")
    return bind_runtime_paths(Config(agents={"coder": agent}, plugins=[str(plugin_root)]), paths)


def _demo_primary(tmp_path: Path, backend: str) -> tuple[RuntimePaths, Config]:
    """Return a primary's paths on ``backend`` and its published config, which loads an empty plugin ``demo``."""
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MINDROOM_WORKER_BACKEND": backend},
    )
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text('{"name": "demo"}')
    config = _config(plugin_root, paths)
    publish_test_plugins(config, paths)
    return paths, config


def _body(module: str = "hello.py", idle_timeout_seconds: float = 600.0) -> dict[str, object]:
    return {"protocol": WORKER_PROTOCOL_VERSION, "module": module, "idle_timeout_seconds": idle_timeout_seconds}


@asynccontextmanager
async def _runner(tmp_path: Path, *, dedicated: bool = True) -> AsyncIterator[_Runner]:
    """Serve the runner app over TCP as a dedicated worker's runner whose config loads the ``demo`` plugin."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text('{"name": "demo"}')
    (plugin_root / "hello.py").write_text(_HELLO)
    (plugin_root / "crash.py").write_text("import sys\nsys.exit(3)\n")
    (plugin_root / "stubborn.py").write_text(_STUBBORN)
    # The worker's Python, without building a virtualenv.
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(Path(sys.executable))
    env = {"MINDROOM_SANDBOX_DEDICATED_WORKER_KEY": _WORKER_KEY} if dedicated else {}
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env=env)
    config = _config(plugin_root, paths)
    initialize_sandbox_runner_app(sandbox_runner_app.app, paths, config=config, runner_token=_TOKEN)
    # The app outlives each test's event loop, so each test starts with services of its own.
    with suppress(KeyError):
        del sandbox_runner_app.app.state.worker_services
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(sandbox_runner_app.app, lifespan="off", log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:  # noqa: ASYNC110 - uvicorn sets a flag, not an event
                await asyncio.sleep(0.01)
        yield _Runner(listener.getsockname()[1], plugin_root, tmp_path / "workspace")
    finally:
        await close_worker_services(sandbox_runner_app.app)
        server.should_exit = True
        await serving
        listener.close()


async def _call(
    method: str,
    url: str,
    body: object = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, object]:
    async with aiohttp.ClientSession() as client, client.request(method, url, json=body, headers=headers) as response:
        text = await response.text()
        return response.status, json.loads(text) if text and response.content_type == "application/json" else text


def _pid_alive(pid: int) -> bool:
    status = Path(f"/proc/{pid}/status")
    return status.exists() and "State:\tZ" not in status.read_text()


async def _until(condition: Callable[[], bool], seconds: float = 10.0) -> None:
    async with asyncio.timeout(seconds):
        while not condition():  # noqa: ASYNC110 - polls a process's state
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_a_service_starts_on_demand_and_answers_only_with_its_secret(tmp_path: Path) -> None:
    """The first ensure starts the program; it serves on its port and refuses a request without the secret."""
    async with _runner(tmp_path) as runner:
        status, state = await _call("POST", runner.url(), _body(), _AUTH)
        assert status == 200
        assert state["state"] == "running"
        assert state["exit_code"] is None
        port = state["port"]
        record = runner.record()
        assert record["runner_url"] == f"http://127.0.0.1:{runner.port}"
        secret = str(record["secret"])
        assert len(secret) >= 32
        assert await _call("GET", f"http://127.0.0.1:{port}/", headers={"X-MindRoom-Service-Secret": secret}) == (
            200,
            "hello",
        )
        assert await _call("GET", f"http://127.0.0.1:{port}/") == (403, "refused")


def _kernel_port_range() -> range:
    low, high = map(int, Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split())
    return range(low, high + 1)


@pytest.mark.asyncio
async def test_a_service_s_port_lies_outside_the_range_the_kernel_numbers_other_sockets_from(tmp_path: Path) -> None:
    """Sockets that bind port 0 or connect get their ports from that range, so none of them can take the service's."""
    async with _runner(tmp_path) as runner:
        _, state = await _call("POST", runner.url(), _body(), _AUTH)
        assert state["state"] == "running"
        assert state["port"] in worker_services_module._SERVICE_PORTS
        assert state["port"] not in _kernel_port_range()


def _service_port_listener() -> socket.socket:
    """Listen on a port of the services' range, taken at random as the runner takes them, so parallel tests rarely meet."""
    ports = worker_services_module._SERVICE_PORTS
    for port in random.sample(ports, len(ports)):
        with suppress(OSError):
            return socket.create_server(("127.0.0.1", port))
    raise AssertionError


@pytest.mark.asyncio
async def test_a_service_whose_first_candidate_port_another_server_holds_runs_on_another(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe passes over a port another process listens on."""
    with _service_port_listener() as other, _service_port_listener() as freed:
        taken, free = other.getsockname()[1], freed.getsockname()[1]
        freed.close()
        monkeypatch.setattr(worker_services_module, "_SERVICE_PORTS", [taken, free])
        async with _runner(tmp_path) as runner:
            _, state = await _call("POST", runner.url(), _body(), _AUTH)
    assert (state["state"], state["port"]) == ("running", free)


@pytest.mark.asyncio
async def test_a_service_s_port_avoids_a_local_port_range_that_reaches_into_the_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator's wider local port range leaves the services only the ports above it."""
    (tmp_path / "ip_local_port_range").write_text("32768\t65000\n")
    monkeypatch.setattr(worker_services_module, "_LOCAL_PORT_RANGE", tmp_path / "ip_local_port_range")
    async with _runner(tmp_path) as runner:
        _, state = await _call("POST", runner.url(), _body(), _AUTH)
    assert state["state"] == "running"
    assert 65000 < state["port"] <= 65535


@pytest.mark.asyncio
async def test_services_starting_together_each_get_a_port_of_their_own(tmp_path: Path) -> None:
    """A start probes while another service has not bound its port yet, so the two rarely pick the same one."""
    async with _runner(tmp_path) as runner:
        (runner.plugin_root / "other.py").write_text(_HELLO.replace("service.json", "other.json"))
        states = await asyncio.gather(
            _call("POST", runner.url(), _body(), _AUTH),
            _call("POST", runner.url("other"), _body("other.py"), _AUTH),
        )
    assert [state["state"] for _, state in states] == ["running", "running"]


@pytest.mark.asyncio
async def test_without_a_free_port_outside_the_kernel_s_range_a_service_gets_one_the_kernel_picks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local port range that covers every service port leaves the kernel to pick one."""
    (tmp_path / "ip_local_port_range").write_text("1024\t65535\n")
    monkeypatch.setattr(worker_services_module, "_LOCAL_PORT_RANGE", tmp_path / "ip_local_port_range")
    async with _runner(tmp_path) as runner:
        _, state = await _call("POST", runner.url(), _body(), _AUTH)
    assert state["state"] == "running"
    assert state["port"] in _kernel_port_range()


@pytest.mark.asyncio
async def test_the_secret_and_the_runner_token_stay_out_of_the_service_s_environment_and_command_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Other processes in the worker read a process's environ and cmdline, so neither holds a secret."""
    monkeypatch.setenv("MINDROOM_SANDBOX_PROXY_TOKEN", _TOKEN)
    async with _runner(tmp_path) as runner:
        await _call("POST", runner.url(), _body(), _AUTH)
        record = runner.record()
        proc = Path(f"/proc/{record['pid']}")
        # Another process of the same user cannot open the service's stdin again to read what it was sent.
        with pytest.raises(OSError, match="No such device or address"):
            (proc / "fd" / "0").open("rb")
        exposed = (proc / "environ").read_bytes() + (proc / "cmdline").read_bytes()
        assert str(record["secret"]).encode() not in exposed
        assert _TOKEN.encode() not in exposed
        assert b"MINDROOM_WORKER_SERVICE_PORT=" in exposed


@pytest.mark.asyncio
async def test_a_second_ensure_reuses_the_running_instance(tmp_path: Path) -> None:
    """One instance per service: ensuring it again answers the same port and starts no second process."""
    async with _runner(tmp_path) as runner:
        _, first = await _call("POST", runner.url(), _body(), _AUTH)
        pid = runner.record()["pid"]
        _, second = await _call("POST", runner.url(), _body(), _AUTH)
        assert second == first
        assert runner.record()["pid"] == pid


@pytest.mark.asyncio
async def test_stop_ends_the_service_and_the_next_ensure_starts_it_again(tmp_path: Path) -> None:
    """A stop ends the program and every process it started; ensuring after it starts a new instance."""
    async with _runner(tmp_path) as runner:
        await _call("POST", runner.url(), _body(), _AUTH)
        record = runner.record()
        await _until((runner.workspace / "child-ready").exists)
        # The stop waits for the processes that end on the TERM signal, not for its whole grace period.
        async with asyncio.timeout(3):
            assert (await _call("DELETE", runner.url(), headers=_AUTH))[0] == 204
        await _until(lambda: not _pid_alive(int(record["child"])))
        assert not _pid_alive(int(record["pid"]))
        assert (runner.workspace / "child-term").exists()
        pid = record["pid"]
        _, state = await _call("POST", runner.url(), _body(), _AUTH)
        assert state["state"] == "running"
        assert runner.record()["pid"] != pid


@pytest.mark.asyncio
async def test_a_service_that_ignores_the_stop_signal_is_killed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A program that outlives its grace period after the TERM signal is killed, with every process it started."""
    monkeypatch.setattr(worker_services_module, "_STOP_SECONDS", 0.2)
    async with _runner(tmp_path) as runner:
        await _call("POST", runner.url("stubborn"), _body("stubborn.py"), _AUTH)
        record = runner.record()
        assert (await _call("DELETE", runner.url("stubborn"), headers=_AUTH))[0] == 204
        assert not _pid_alive(int(record["pid"]))
        await _until(lambda: not _pid_alive(int(record["child"])))


@pytest.mark.asyncio
async def test_each_ensure_resets_the_idle_timer(tmp_path: Path) -> None:
    """A service ensured more often than its idle timeout keeps running, even when an ensure waited on another one."""
    async with _runner(tmp_path) as runner:
        services = worker_services(sandbox_runner_app.app)
        for _ in range(4):
            await _call("POST", runner.url(), _body(idle_timeout_seconds=0.6), _AUTH)
            await asyncio.sleep(0.3)
        instance = services.instance("demo", "hello")
        # The timeout passes while another call holds the services, and an ensure resets it before that call ends.
        async with services._lock:
            await asyncio.sleep(0.8)
            services.touch("demo", "hello")
        await asyncio.sleep(0.05)
        assert services.instance("demo", "hello") is instance
        assert _pid_alive(int(runner.record()["pid"]))


@pytest.mark.asyncio
async def test_an_idle_service_stops_after_its_timeout(tmp_path: Path) -> None:
    """Without an ensure for its idle timeout, the instance ends and is forgotten."""
    async with _runner(tmp_path) as runner:
        # Long enough for the program to start on a busy machine.
        await _call("POST", runner.url(), _body(idle_timeout_seconds=2.0), _AUTH)
        pid = int(runner.record()["pid"])
        await _until(lambda: not _pid_alive(pid), seconds=20)
        assert worker_services(sandbox_runner_app.app).instance("demo", "hello") is None


@pytest.mark.asyncio
async def test_a_crashed_service_is_reported_and_not_restarted(tmp_path: Path) -> None:
    """A program that exits is reported with its exit code, and ensuring it again starts nothing until a stop."""
    async with _runner(tmp_path) as runner:
        _, state = await _call("POST", runner.url("crash"), _body("crash.py"), _AUTH)
        assert state["state"] == "exited"
        assert state["exit_code"] == 3
        process = worker_services(sandbox_runner_app.app).instance("demo", "crash").process  # type: ignore[union-attr]
        _, again = await _call("POST", runner.url("crash"), _body("crash.py"), _AUTH)
        assert again == state
        assert worker_services(sandbox_runner_app.app).instance("demo", "crash").process is process  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_a_changed_program_starts_again_on_the_next_ensure(tmp_path: Path) -> None:
    """An instance runs the program file it started from; an ensure after the file changed restarts it."""
    async with _runner(tmp_path) as runner:
        await _call("POST", runner.url(), _body(), _AUTH)
        pid = int(runner.record()["pid"])
        (runner.plugin_root / "hello.py").write_text(_HELLO + "\n# changed\n")
        _, state = await _call("POST", runner.url(), _body(), _AUTH)
        assert state["state"] == "running"
        assert not _pid_alive(pid)
        assert runner.record()["pid"] != pid


@pytest.mark.asyncio
async def test_the_runner_stopping_ends_its_services(tmp_path: Path) -> None:
    """The runner's shutdown ends every instance."""
    async with _runner(tmp_path) as runner:
        async with sandbox_runner_app._lifespan(sandbox_runner_app.app):
            await _call("POST", runner.url(), _body(), _AUTH)
            pid = int(runner.record()["pid"])
        assert not _pid_alive(pid)


@pytest.mark.asyncio
async def test_the_runner_refuses_what_it_cannot_run(tmp_path: Path) -> None:
    """A missing plugin, a program outside it, another protocol, a non-finite timeout, no token: refusals, no process."""
    async with _runner(tmp_path) as runner:
        status, answer = await _call("POST", runner.url(plugin="other"), _body(), _AUTH)
        assert status == 404
        assert "not loaded in this worker" in answer["detail"]
        (runner.plugin_root.parent / "outside.py").write_text(_HELLO)
        status, answer = await _call("POST", runner.url(), _body("../outside.py"), _AUTH)
        assert status == 400
        assert "no file in plugin" in answer["detail"]
        status, answer = await _call("POST", runner.url(), {**_body(), "protocol": 0}, _AUTH)
        assert status == 400
        assert "worker protocol" in answer["detail"]
        for idle in (float("inf"), float("nan")):
            assert (await _call("POST", runner.url(), _body(idle_timeout_seconds=idle), _AUTH))[0] == 422
        assert (await _call("POST", runner.url(), _body()))[0] == 401
        assert not (runner.workspace / "service.json").exists()


@pytest.mark.asyncio
async def test_a_plugin_whose_folder_this_worker_lacks_is_refused(tmp_path: Path) -> None:
    """The request's config names the plugin, but its folder is missing here: a clear refusal."""
    async with _runner(tmp_path) as runner:
        shutil.rmtree(runner.plugin_root)
        status, answer = await _call("POST", runner.url(), _body(), _AUTH)
        assert status == 404
        assert answer["detail"] == (
            "Plugin 'demo' is not loaded in this worker: its folder is missing here, or it failed to load."
        )


@pytest.mark.asyncio
async def test_a_stop_refuses_a_start_that_arrived_before_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A start still preparing when a stop arrives, such as a reload's, never starts the service after that stop."""
    preparing, release = threading.Event(), threading.Event()
    program = worker_services_module._program

    def slow_program(*args: object) -> object:
        preparing.set()
        release.wait(10)
        return program(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(worker_services_module, "_program", slow_program)
    async with _runner(tmp_path) as runner:
        start = asyncio.create_task(_call("POST", runner.url(), _body(), _AUTH))
        await _until(preparing.is_set)
        assert (await _call("DELETE", runner.url(), headers=_AUTH))[0] == 204
        release.set()
        status, answer = await start
        assert (status, answer["detail"]) == (409, "The service was stopped while it started.")
        assert worker_services(sandbox_runner_app.app).instance("demo", "hello") is None
        assert (await _call("POST", runner.url(), _body(), _AUTH))[1]["state"] == "running"


@pytest.mark.asyncio
async def test_any_valid_plugin_name_runs_services(tmp_path: Path) -> None:
    """Plugin names may start with an underscore and run longer than service names."""
    name = "_" + "x" * 80
    async with _runner(tmp_path) as runner:
        (runner.plugin_root / "mindroom.plugin.json").write_text(json.dumps({"name": name}))
        status, state = await _call("POST", runner.url(plugin=name), _body(), _AUTH)
        assert (status, state["state"]) == (200, "running")
        assert (await _call("DELETE", runner.url(plugin=name), headers=_AUTH))[0] == 204


@pytest.mark.asyncio
async def test_only_a_dedicated_worker_s_runner_runs_services(tmp_path: Path) -> None:
    """The shared runner's loopback is every agent's, so it runs none."""
    async with _runner(tmp_path, dedicated=False) as runner:
        status, answer = await _call("POST", runner.url(), _body(), _AUTH)
        assert status == 403
        assert answer["detail"] == "Only a dedicated worker's runner runs plugin services."


# What the plugin ``demo`` declares, and the worker the agent ``coder``'s turns use for Alice.
_SERVICE = WorkerService("hello", "hello.py", idle_timeout_seconds=300)
_TARGET = resolve_worker_target(
    "shared",
    "coder",
    ToolExecutionIdentity("matrix", "coder", "@alice:localhost", None, None, None, None),
)


class _Workers:
    """A dedicated backend whose one worker is the test's runner: an ensure starts it, a touch only finds it."""

    cleanup_locator: str | None = None

    def __init__(self, port: int) -> None:
        self.handle = WorkerHandle(
            worker_id="worker-1",
            worker_key=_WORKER_KEY,
            endpoint=f"http://127.0.0.1:{port}/api/sandbox-runner/execute",
            auth_token=_TOKEN,
            status="ready",
            backend_name="docker",
            last_used_at=0.0,
            created_at=0.0,
        )
        self.running = False
        self.touches = 0

    def ensure_worker(self, spec: WorkerSpec, *, progress_sink: object = None) -> WorkerHandle:
        del progress_sink
        assert spec.worker_key == _WORKER_KEY
        self.running = True
        return self.handle

    def touch_worker(self, worker_key: str) -> WorkerHandle | None:
        assert worker_key == _WORKER_KEY
        self.touches += 1
        return self.handle if self.running else None


@pytest.fixture(autouse=True)
def _forget_started_services() -> Iterator[None]:
    """Leave no service a test started for a later plugin load to stop."""
    yield
    plugin_worker_services._started.clear()


def _primary(tmp_path: Path, runner: _Runner, monkeypatch: pytest.MonkeyPatch, workers: _Workers) -> PluginContext:
    """Return the plugin ``demo``'s context in a primary whose Docker workers are ``workers``."""
    env = {"MINDROOM_WORKER_BACKEND": "docker"}
    paths = resolve_runtime_paths(
        config_path=tmp_path / "primary.yaml",
        storage_path=tmp_path / "primary",
        process_env=env,
    )
    config = _config(runner.plugin_root, paths)
    publish_test_plugins(config, paths)
    monkeypatch.setattr(sandbox_proxy, "lease_primary_worker_manager", lambda *_a, **_k: nullcontext(workers))
    return build_plugin_context("demo", config, paths, lambda: config)


@pytest.mark.asyncio
async def test_a_plugin_runs_its_service_in_the_agent_s_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The primary starts the service in the target's worker, reuses it on the next ensure, and stops it."""
    async with _runner(tmp_path) as runner:
        context = _primary(tmp_path, runner, monkeypatch, _Workers(runner.port))
        state = await ensure_worker_service(context, _SERVICE, _TARGET)
        assert state.state == "running"
        assert await ensure_worker_service(context, _SERVICE, _TARGET) == state
        instance = worker_services(sandbox_runner_app.app).instance("demo", "hello")
        assert instance is not None
        assert instance.port == state.port
        assert instance.idle_timeout_seconds == 300

        await stop_worker_service(context, _SERVICE, _TARGET)
        assert worker_services(sandbox_runner_app.app).instance("demo", "hello") is None
        # A stopped service is not stopped again at the next plugin load.
        assert plugin_worker_services._started == {}


@pytest.mark.asyncio
async def test_the_primary_reports_what_keeps_a_service_from_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each reason a service cannot run fails with that reason; stopping in a stopped worker starts nothing."""
    async with _runner(tmp_path) as runner:
        workers = _Workers(runner.port)
        context = _primary(tmp_path, runner, monkeypatch, workers)
        await stop_worker_service(context, _SERVICE, _TARGET)
        assert not workers.running

        with pytest.raises(WorkerServiceError, match=r"refused: The service program 'missing\.py' is no file"):
            await ensure_worker_service(context, WorkerService("hello", "missing.py"), _TARGET)
        with pytest.raises(WorkerServiceError, match=r"refused: body\.module: String should have at most 4096 char"):
            await ensure_worker_service(context, WorkerService("hello", "x" * 5000), _TARGET)
        crashed = await ensure_worker_service(context, WorkerService("crash", "crash.py"), _TARGET)
        assert (crashed.state, crashed.exit_code) == ("exited", 3)

        unresolved = resolve_worker_target("user", "coder", replace(_TARGET.execution_identity, requester_id=None))
        with pytest.raises(WorkerServiceError, match="The agent's worker is unavailable: Worker scope 'user'"):
            await ensure_worker_service(context, _SERVICE, unresolved)
        with pytest.raises(WorkerServiceError, match=r"tool 'worker service\.stop'"):
            await stop_worker_service(context, _SERVICE, unresolved)

        async def garbled(*_args: object) -> bytes:
            return b'{"state": "gone"}'

        with monkeypatch.context() as patched:
            patched.setattr(plugin_worker_services, "_send", garbled)
            with pytest.raises(WorkerServiceError, match="answered something other than a service state"):
                await ensure_worker_service(context, _SERVICE, _TARGET)

        workers.handle = replace(workers.handle, status="starting")
        with pytest.raises(WorkerServiceError, match=r"The agent's worker is not ready\."):
            await ensure_worker_service(context, _SERVICE, _TARGET)
        with socket.socket() as closed:
            closed.bind(("127.0.0.1", 0))
            endpoint = f"http://127.0.0.1:{closed.getsockname()[1]}/api/sandbox-runner/execute"
        workers.handle = replace(workers.handle, status="ready", endpoint=endpoint)
        with pytest.raises(WorkerServiceError, match="The agent's worker is unavailable: ConnectError"):
            await ensure_worker_service(context, _SERVICE, _TARGET)
        # A stop that fails keeps the service recorded, so the next plugin load stops it.
        with pytest.raises(WorkerServiceError, match="The agent's worker is unavailable: ConnectError"):
            await stop_worker_service(context, _SERVICE, _TARGET)
        assert (_WORKER_KEY, "demo", "hello") in plugin_worker_services._started
        workers.handle = replace(workers.handle, auth_token=None)
        with pytest.raises(WorkerServiceError, match="unavailable: MINDROOM_SANDBOX_PROXY_TOKEN must be set"):
            await ensure_worker_service(context, _SERVICE, _TARGET)


# The lifespan of a plugin that starts its service as it starts, for Alice's turns of the agent ``coder``.
_LIFECYCLE = """
from contextlib import asynccontextmanager

from mindroom.plugin_api import WorkerService, ensure_worker_service
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target

TARGET = resolve_worker_target(
    "shared", "coder", ToolExecutionIdentity("matrix", "coder", "@alice:localhost", None, None, None, None)
)


@asynccontextmanager
async def lifespan(lifecycle):
    await ensure_worker_service(lifecycle.context(), WorkerService("hello", "hello.py"), TARGET)
    yield
"""


@pytest.mark.asyncio
async def test_a_service_the_new_code_starts_as_it_loads_keeps_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A load stops the services the replaced code started before the new code starts its own, at startup too."""
    async with _runner(tmp_path) as runner:
        (runner.plugin_root / "mindroom.plugin.json").write_text('{"name": "demo", "lifecycle_module": "lifecycle.py"}')
        (runner.plugin_root / "lifecycle.py").write_text(_LIFECYCLE)
        context = _primary(tmp_path, runner, monkeypatch, _Workers(runner.port))
        config = context.config
        lifespans = PluginLifespans(context.runtime_paths, lambda: config)
        services = worker_services(sandbox_runner_app.app)

        await lifespans.apply_published()
        first = services.instance("demo", "hello")
        assert first is not None
        publish_test_plugins(config, context.runtime_paths)
        await lifespans.apply_published()
        second = services.instance("demo", "hello")
        assert second is not None
        assert second is not first
        assert second.process.returncode is None
        await lifespans.stop()
        assert services.instance("demo", "hello") is None


@pytest.mark.parametrize("backend", ["static_runner", ""])
@pytest.mark.asyncio
async def test_without_a_dedicated_worker_backend_a_service_never_starts(tmp_path: Path, backend: str) -> None:
    """Nothing falls back to the primary: the shared runner and no worker at all fail with the setting to change."""
    paths, config = _demo_primary(tmp_path, backend)
    context = build_plugin_context("demo", config, paths, lambda: config)
    expected = "Worker services need a dedicated worker backend: set MINDROOM_WORKER_BACKEND to docker or kubernetes."
    with pytest.raises(WorkerServiceError, match=re.escape(expected)):
        await ensure_worker_service(context, _SERVICE, _TARGET)
    with pytest.raises(WorkerServiceError, match=re.escape(expected)):
        await stop_worker_service(context, _SERVICE, _TARGET)


@pytest.mark.parametrize(
    ("name", "idle"),
    [
        ("Hello", 1.0),
        ("_cache", 1.0),
        ("a/b", 1.0),
        ("", 1.0),
        ("hello", 0.0),
        ("hello", float("inf")),
        ("hello", float("nan")),
    ],
)
def test_a_service_declaration_refuses_a_bad_name_or_timeout(name: str, idle: float) -> None:
    """A name the runner's routes cannot carry, or a timeout not positive and finite, fails where it is declared."""
    with pytest.raises(ValueError, match=r"(?i)worker service"):
        WorkerService(name, "hello.py", idle_timeout_seconds=idle)


@pytest.mark.asyncio
async def test_a_service_that_fails_to_stop_holds_up_no_other(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A plugin load or shutdown stops every started service, even when one worker cannot be reached, and keeps that one."""
    stopped: list[str] = []

    async def stop(_paths: object, _worker_key: str, _plugin: str, name: str) -> None:
        stopped.append(name)
        if name == "broken":
            msg = "worker gone"
            raise WorkerServiceError(msg)

    monkeypatch.setattr(plugin_worker_services, "_stop_recorded", stop)
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    for name in ("broken", "hello"):
        plugin_worker_services._started[(_WORKER_KEY, "demo", name)] = paths
    await plugin_worker_services.stop_started_worker_services()
    assert stopped == ["broken", "hello"]
    # The service that failed to stop stays recorded, so the next load or shutdown stops it.
    assert list(plugin_worker_services._started) == [(_WORKER_KEY, "demo", "broken")]


@pytest.mark.asyncio
async def test_a_plugin_load_stops_a_service_whose_ensure_was_under_way(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A load waits for an ensure still starting the agent's worker, then stops the service that ensure started."""
    async with _runner(tmp_path) as runner:
        context = _primary(tmp_path, runner, monkeypatch, _Workers(runner.port))
        starting, release = threading.Event(), threading.Event()
        target_worker = plugin_worker_services._target_worker

        def slow_worker_start(*args: object, **kwargs: object) -> object:
            starting.set()
            release.wait(10)
            return target_worker(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(plugin_worker_services, "_target_worker", slow_worker_start)
        ensure = asyncio.create_task(ensure_worker_service(context, _SERVICE, _TARGET))
        await _until(starting.is_set)
        load = asyncio.create_task(plugin_worker_services.stop_started_worker_services())
        await asyncio.sleep(0.2)
        assert not load.done()
        release.set()
        assert (await ensure).state == "running"
        await load
        assert worker_services(sandbox_runner_app.app).instance("demo", "hello") is None


class _AnyWorkers:
    """Dedicated workers for any key, each at a runner of its own, recording the keys a stop looks up."""

    def __init__(self) -> None:
        self.touched: list[str] = []

    def _handle(self, worker_key: str) -> WorkerHandle:
        return WorkerHandle(
            worker_id=worker_key,
            worker_key=worker_key,
            endpoint=f"http://{abs(hash(worker_key))}.workers/api/sandbox-runner/execute",
            auth_token=_TOKEN,
            status="ready",
            backend_name="docker",
            last_used_at=0.0,
            created_at=0.0,
        )

    def ensure_worker(self, spec: WorkerSpec, *, progress_sink: object = None) -> WorkerHandle:
        del progress_sink
        return self._handle(spec.worker_key)

    def touch_worker(self, worker_key: str) -> WorkerHandle | None:
        self.touched.append(worker_key)
        return self._handle(worker_key)


@pytest.mark.asyncio
async def test_a_plugin_load_stops_a_service_in_the_worker_it_started_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service a subagent's call started stops in that subagent's own worker, though the load runs outside the call."""
    paths, config = _demo_primary(tmp_path, "docker")
    context = build_plugin_context("demo", config, paths, lambda: config)
    workers = _AnyWorkers()
    monkeypatch.setattr(sandbox_proxy, "lease_primary_worker_manager", lambda *_a, **_k: nullcontext(workers))
    sent: list[tuple[str, str]] = []

    async def send(method: str, endpoint: object, path: str, _body: object) -> bytes:
        sent.append((method, f"{endpoint.root}/{path}"))  # type: ignore[attr-defined]
        return b'{"state": "running", "port": 1, "exit_code": null}'

    monkeypatch.setattr(plugin_worker_services, "_send", send)
    identity = ToolExecutionIdentity("matrix", "coder", "@alice:localhost", None, None, None, "session")
    subagent = "a" * 32
    with dedicated_worker_placement("session", DedicatedWorkerPlacement(subagent)):
        await ensure_worker_service(context, _SERVICE, resolve_worker_target("shared", "coder", identity))
    await plugin_worker_services.stop_started_worker_services()

    subagent_key = subagent_worker_key(_WORKER_KEY, subagent)
    root = f"http://{abs(hash(subagent_key))}.workers/api/sandbox-runner/plugin-services"
    assert workers.touched == [subagent_key]
    assert sent == [("POST", f"{root}/demo/hello"), ("DELETE", f"{root}/demo/hello")]


@pytest.mark.asyncio
async def test_an_ensure_during_a_stop_keeps_its_service_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service an ensure records while a load still stops its old instance stays recorded for the next load."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    key = (_WORKER_KEY, "demo", "hello")
    stopping, release = asyncio.Event(), asyncio.Event()

    async def send(method: str, _endpoint: object, _path: str, _body: object) -> bytes:
        assert method == "DELETE"
        stopping.set()
        await release.wait()
        return b""

    monkeypatch.setattr(plugin_worker_services, "_recorded_worker", lambda *_args: object())
    monkeypatch.setattr(plugin_worker_services, "_send", send)
    plugin_worker_services._started[key] = paths
    load = asyncio.create_task(plugin_worker_services.stop_started_worker_services())
    await stopping.wait()
    # An ensure of the new code records the service while the old instance is still stopping.
    plugin_worker_services._started[key] = paths
    release.set()
    await load
    assert plugin_worker_services._started == {key: paths}


# A service that echoes each WebSocket text message with its pid and path, refusing a handshake without its secret.
# The worker's Python here has only the standard library, so it speaks the protocol's few frames itself.
_ECHO = """
import base64, gzip, hashlib, os, socketserver, struct, sys, time

secret = sys.stdin.readline().strip()


class Echo(socketserver.StreamRequestHandler):
    def handle(self):
        path = self.rfile.readline().split()[1].decode()
        headers = {}
        while (line := self.rfile.readline().strip()):
            name, _, value = line.decode().partition(":")
            # The first copy counts, so a forged one passed on ahead of the runner's own fails the check.
            headers.setdefault(name.lower(), value.strip())
        if headers.get("x-mindroom-service-secret") != secret:
            self.wfile.write(b"HTTP/1.1 403 Forbidden\\r\\nContent-Length: 0\\r\\n\\r\\n")
            return
        if path == "/hop":
            self.wfile.write(b"HTTP/1.1 302 Found\\r\\nLocation: /a\\r\\nContent-Length: 0\\r\\n\\r\\n")
            return
        if path == "/gzip":
            body = gzip.compress(b"compressed")
            self.wfile.write(f"HTTP/1.1 200 OK\\r\\nContent-Encoding: gzip\\r\\nContent-Length: {len(body)}\\r\\n\\r\\n".encode() + body)
            return
        if "upgrade" not in headers:
            # A slow answer, as a stream of events is, whose second part comes after the idle timeout.
            self.wfile.write(b"HTTP/1.1 200 OK\\r\\nConnection: close\\r\\n\\r\\nfirst;")
            self.wfile.flush()
            time.sleep(1.5)
            self.wfile.write(f"second from {os.getpid()}".encode())
            return
        accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        self.wfile.write(b"HTTP/1.1 101 Switching Protocols\\r\\nUpgrade: websocket\\r\\nConnection: Upgrade\\r\\nSec-WebSocket-Accept: " + accept + b"\\r\\n\\r\\n")
        while True:
            first, second = self.rfile.read(2)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self.rfile.read(2))[0]
            mask = self.rfile.read(4)
            data = bytes(byte ^ mask[i % 4] for i, byte in enumerate(self.rfile.read(size)))
            if first & 0x0F == 8:
                self.wfile.write(bytes([0x88, len(data)]) + data)
                return
            # At /headers it echoes the handshake headers an app reads instead.
            seen = "|".join(headers.get(name, "") for name in ("cookie", "origin", "user-agent"))
            reply = (seen if path == "/headers" else f"{os.getpid()}:{path}:{data.decode()}").encode()
            self.wfile.write(bytes([0x81, len(reply)]) + reply)


socketserver.ThreadingTCPServer(("127.0.0.1", int(os.environ["MINDROOM_WORKER_SERVICE_PORT"])), Echo).serve_forever()
"""
_REFUSED = "x-mindroom-relay-refused"


async def _relay(
    tmp_path: Path,
    runner: _Runner,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[PluginContext, _Workers, WorkerRelay]:
    """Start the agent's worker, the test's runner, through an ensure of ``hello``, and open the relay for ``demo``."""
    workers = _Workers(runner.port)
    context = _primary(tmp_path, runner, monkeypatch, workers)
    assert (await ensure_worker_service(context, _SERVICE, _TARGET)).state == "running"
    return context, workers, await open_worker_relay(context.runtime_paths, _TARGET, context.plugin_name)


async def _get(
    relay: WorkerRelay,
    path: str,
    *,
    service: str | None = None,
    port: int | None = None,
) -> tuple[int, str, bool]:
    async with relay.http("GET", path, service=service, port=port) as answer:
        return answer.status, await answer.text(), _REFUSED in answer.headers


@pytest.mark.asyncio
async def test_a_route_reaches_its_own_service_by_name_with_the_secret_only_the_runner_adds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The service answers through the relay, which sends its secret, never the route's copy of it or of the token."""
    async with _runner(tmp_path) as runner:
        _, _, relay = await _relay(tmp_path, runner, monkeypatch)
        assert await _get(relay, "/", service="hello") == (200, "hello", False)
        forged = [("X-MindRoom-Service-Secret", "forged"), ("X-Mindroom-Sandbox-Token", "forged")]
        async with relay.http("GET", "/", service="hello", headers=forged) as answer:
            assert (answer.status, await answer.text()) == (200, "hello")
        # Its path stays below the service, dot segments included.
        assert await _get(relay, f"/../../../ports/{runner.port}/", service="hello") == (200, "hello", False)
        with pytest.raises(ValueError, match="exactly one of service and port"):
            await _get(relay, "/")
        with pytest.raises(ValueError, match="exactly one of service and port"):
            await _get(relay, "/", service="hello", port=1)
        with pytest.raises(ValueError, match="is no worker service name"):
            await _get(relay, "/", service="../ports")


@pytest.mark.asyncio
async def test_a_route_never_reaches_another_plugins_service_or_one_that_is_not_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another plugin's relay and a stopped service get the relay's 503, telling the route to ensure the service."""
    async with _runner(tmp_path) as runner:
        context, _, relay = await _relay(tmp_path, runner, monkeypatch)
        other = await open_worker_relay(context.runtime_paths, _TARGET, "other")
        status, text, marked = await _get(other, "/", service="hello")
        assert (status, marked) == (503, True)
        assert json.loads(text) == {
            "detail": "Service 'hello' of plugin 'other' is not running in this worker.",
        }
        await stop_worker_service(context, _SERVICE, _TARGET)
        assert (await _get(relay, "/", service="hello"))[::2] == (503, True)
        crash = WorkerService("crash", "crash.py")
        assert (await ensure_worker_service(context, crash, _TARGET)).state == "exited"
        assert (await _get(relay, "/", service="crash"))[::2] == (503, True)
        with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
            async with relay.websocket("/", service="hello"):
                pass
        assert refused.value.status == 503
        assert refused.value.headers is not None
        assert refused.value.headers[_REFUSED] == "1"


@pytest.mark.asyncio
async def test_a_relayed_stream_keeps_its_service_and_its_worker_in_use_while_it_lasts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WebSocket outlasts the service's idle timeout, and idle cleanup refreshes its worker until it closes."""
    async with _runner(tmp_path) as runner:
        (runner.plugin_root / "echo.py").write_text(_ECHO)
        context, workers, relay = await _relay(tmp_path, runner, monkeypatch)
        echo = WorkerService("echo", "echo.py", idle_timeout_seconds=0.5)
        assert (await ensure_worker_service(context, echo, _TARGET)).state == "running"
        services = worker_services(sandbox_runner_app.app)
        instance = services.instance("demo", "echo")
        assert instance is not None
        async with relay.websocket("/a/b?c=d", service="echo") as upstream:
            await upstream.send_str("one")
            assert (await upstream.receive_str()) == f"{instance.process.pid}:/a/b?c=d:one"
            await asyncio.sleep(1.5)
            assert services.instance("demo", "echo") is instance
            await upstream.send_str("two")
            assert (await upstream.receive_str()).endswith(":two")
            touches = workers.touches
            touch_workers_in_flight(workers)  # type: ignore[arg-type]
            assert workers.touches == touches + 1
        touch_workers_in_flight(workers)  # type: ignore[arg-type]
        assert workers.touches == touches + 1
        # An answer passes as the service sent it, compressed or not.
        async with relay.http("GET", "/gzip", service="echo") as answer:
            assert answer.headers["Content-Encoding"] == "gzip"
            assert gzip.decompress(await answer.read()) == b"compressed"
        # A slow answer outlasts the idle timeout too.
        async with relay.http("GET", "/slow", service="echo") as answer:
            assert await answer.text() == f"first;second from {instance.process.pid}"
        await _until(lambda: services.instance("demo", "echo") is None)


@pytest.mark.asyncio
async def test_a_route_passes_the_headers_it_chooses_to_its_services_websocket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handshake carries the route's cookie, origin, and user agent, and the secret only the runner adds."""
    async with _runner(tmp_path) as runner:
        (runner.plugin_root / "echo.py").write_text(_ECHO)
        context, _, relay = await _relay(tmp_path, runner, monkeypatch)
        assert (await ensure_worker_service(context, WorkerService("echo", "echo.py"), _TARGET)).state == "running"
        async with relay.websocket("/headers", service="echo") as upstream:
            await upstream.send_str("which")
            assert (await upstream.receive_str()) == "||"
        headers = [
            ("Cookie", "app=1"),
            ("Origin", "http://app.demo.localhost"),
            ("User-Agent", "browser"),
            ("X-MindRoom-Service-Secret", "forged"),
            ("X-Mindroom-Sandbox-Token", "forged"),
        ]
        async with relay.websocket("/headers", service="echo", headers=headers) as upstream:
            await upstream.send_str("which")
            assert (await upstream.receive_str()) == "app=1|http://app.demo.localhost|browser"


@pytest.mark.asyncio
async def test_a_route_gets_its_services_websocket_redirect_without_following_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handshake the service redirects reaches the route as the redirect, and nothing follows it with the secret."""
    async with _runner(tmp_path) as runner:
        (runner.plugin_root / "echo.py").write_text(_ECHO)
        context, _, relay = await _relay(tmp_path, runner, monkeypatch)
        assert (await ensure_worker_service(context, WorkerService("echo", "echo.py"), _TARGET)).state == "running"
        with pytest.raises(aiohttp.WSServerHandshakeError) as redirected:
            async with relay.websocket("/hop", service="echo"):
                pass
        assert redirected.value.headers is not None
        assert (redirected.value.status, redirected.value.headers["Location"]) == (302, "/a")
        assert _REFUSED not in redirected.value.headers


@asynccontextmanager
async def _other_server(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[list[object]]:
    """Serve on the next port probed for a service, as another socket can take it before the service binds it.

    Yields the connections it gets.
    """
    connections: list[object] = []

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        del reader
        connections.append(writer.get_extra_info("peername"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    async with await asyncio.start_server(answer, "127.0.0.1", 0) as other:
        monkeypatch.setattr(worker_services_module, "_free_loopback_port", lambda: other.sockets[0].getsockname()[1])
        yield connections


async def _websocket_refusal(relay: WorkerRelay, service: str) -> int:
    with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
        async with relay.websocket("/", service=service):
            pass
    assert refused.value.headers is not None
    assert refused.value.headers[_REFUSED] == "1"
    return refused.value.status


@pytest.mark.asyncio
async def test_a_service_whose_port_another_server_took_exits_and_the_relay_never_reaches_that_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another server that listens on the port probed for a service is never reported as it nor relayed to."""
    async with _runner(tmp_path) as runner:
        (runner.plugin_root / "echo.py").write_text(_ECHO)
        context, _, relay = await _relay(tmp_path, runner, monkeypatch)
        async with _other_server(monkeypatch) as connections:
            state = await ensure_worker_service(context, WorkerService("echo", "echo.py"), _TARGET)
            assert (state.state, state.exit_code) == ("exited", 1)
            assert (await _get(relay, "/", service="echo"))[::2] == (503, True)
            assert await _websocket_refusal(relay, "echo") == 503
            assert connections == []


@pytest.mark.asyncio
async def test_the_relay_never_reaches_another_server_on_the_port_of_a_service_that_is_still_starting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service that has not bound its port yet gets the relay's 502, and its 403 while another server holds it."""
    async with _runner(tmp_path) as runner:
        (runner.plugin_root / "waiting.py").write_text("import sys, time\n\nsys.stdin.readline()\ntime.sleep(600)\n")
        context, _, relay = await _relay(tmp_path, runner, monkeypatch)
        monkeypatch.setattr(worker_services_module, "_START_SECONDS", 0.5)
        waiting = WorkerService("waiting", "waiting.py")
        assert (await ensure_worker_service(context, waiting, _TARGET)).state == "starting"
        assert (await _get(relay, "/", service="waiting"))[::2] == (502, True)
        await stop_worker_service(context, waiting, _TARGET)
        async with _other_server(monkeypatch) as connections:
            state = await ensure_worker_service(context, waiting, _TARGET)
            assert state.state == "starting"
            status, text, marked = await _get(relay, "/", service="waiting")
            assert (status, marked) == (403, True)
            assert json.loads(text) == {"detail": f"The server on port {state.port} is not the service's own."}
            assert await _websocket_refusal(relay, "waiting") == 403
            assert connections == []


@pytest.mark.asyncio
async def test_the_relay_reads_a_services_holders_again_only_once_another_socket_listens_on_its_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relaying to a running service reads which processes hold its port once, and again once another socket joins it."""
    reads: list[int] = []
    read = worker_preview.group_sockets

    def counted(sockets: frozenset[int], group: int) -> frozenset[int]:
        reads.append(group)
        return read(sockets, group)

    monkeypatch.setattr(worker_preview, "group_sockets", counted)
    async with _runner(tmp_path) as runner:
        context, _, relay = await _relay(tmp_path, runner, monkeypatch)
        for _ in range(3):
            assert await _get(relay, "/", service="hello") == (200, "hello", False)
        assert len(reads) == 1
        state = await ensure_worker_service(context, _SERVICE, _TARGET)
        # A socket outside the service's process group listens on another loopback address of the same port.
        with socket.create_server(("127.0.0.2", state.port)):
            status, _, marked = await _get(relay, "/", service="hello")
        assert (status, marked, len(reads)) == (403, True, 2)


# A service whose server is a process it starts, as a launcher's is, while the program itself only waits for it.
_LAUNCHER = """
import subprocess, sys

SERVER = "import os, socket; server = socket.create_server(('127.0.0.1', int(os.environ['MINDROOM_WORKER_SERVICE_PORT']))); server.accept()"
subprocess.run([sys.executable, "-c", SERVER])
"""


@pytest.mark.asyncio
async def test_a_service_runs_once_a_process_it_started_listens_on_its_port(tmp_path: Path) -> None:
    """The listener may be another process of the service's process group."""
    async with _runner(tmp_path) as runner:
        (runner.plugin_root / "launcher.py").write_text(_LAUNCHER)
        _, state = await _call("POST", runner.url("launcher"), _body("launcher.py"), _AUTH)
        assert state["state"] == "running"


def _hide_holders(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A process table that lists no process this runner may read.
    monkeypatch.setattr(listeners, "_PROC", tmp_path / "proc")
    (tmp_path / "proc").mkdir()


def _end_holders(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    del tmp_path

    def ended(pid: int) -> int:
        raise ProcessLookupError(pid)

    monkeypatch.setattr(listeners.os, "getpgid", ended)


@pytest.mark.parametrize("unknown", [_hide_holders, _end_holders], ids=["unreadable", "ended"])
@pytest.mark.asyncio
async def test_a_listener_whose_holders_the_runner_cannot_place_is_not_the_service_s(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unknown: Callable[[pytest.MonkeyPatch, Path], None],
) -> None:
    """A socket on the service's port held by processes the runner cannot read, or that end as it looks, is not its own."""
    monkeypatch.setattr(worker_services_module, "_START_SECONDS", 0.5)
    unknown(monkeypatch, tmp_path)
    async with _runner(tmp_path) as runner:
        _, state = await _call("POST", runner.url(), _body(), _AUTH)
        assert state["state"] == "starting"


@pytest.mark.asyncio
async def test_a_route_reaches_a_server_the_agent_started_by_port_and_nothing_else(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port: int,
) -> None:
    """A server a worker process runs answers by port; the runner's own port and an unowned server are refused."""
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("the agent's app")
    (site / "sub").mkdir()
    command = [
        sys.executable,
        "-m",
        "http.server",
        "--bind",
        "127.0.0.1",
        "--directory",
        str(site),
        str(unused_tcp_port),
    ]
    server = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with _runner(tmp_path) as runner:
            _, _, relay = await _relay(tmp_path, runner, monkeypatch)
            await _until(lambda: unused_tcp_port in {listener.port for listener in listeners.listening_sockets()})
            assert await _get(relay, "/index.html", port=unused_tcp_port) == (200, "the agent's app", False)
            # A redirect reaches the route as the app sent it.
            async with relay.http("GET", "/sub", port=unused_tcp_port) as answer:
                assert (answer.status, answer.headers["Location"]) == (301, "/sub/")
            with pytest.raises(aiohttp.WSServerHandshakeError) as redirected:
                async with relay.websocket("/sub", port=unused_tcp_port):
                    pass
            assert redirected.value.headers is not None
            assert (redirected.value.status, redirected.value.headers["Location"]) == (301, "/sub/")
            assert unused_tcp_port in await relay.ports()
            status, text, marked = await _get(relay, "/", port=runner.port)
            assert (status, marked) == (403, True)
            assert "this runner's own" in text
            proc = tmp_path / "proc"
            proc.mkdir()
            monkeypatch.setattr(listeners, "_PROC", proc)
            monkeypatch.setattr("mindroom.api.worker_preview._found_workers_sockets", {})
            assert (await _get(relay, "/", port=unused_tcp_port))[::2] == (403, True)
            assert unused_tcp_port not in await relay.ports()

            def cannot_tell() -> list[object]:
                raise OSError

            monkeypatch.setattr("mindroom.api.worker_preview.listening_sockets", cannot_tell)
            with pytest.raises(HTTPException) as refused:
                await relay.ports()
            assert refused.value.status_code == 403
            assert refused.value.headers == {_REFUSED: "1"}
            assert "can't tell which processes hold its ports" in str(refused.value.detail)
    finally:
        server.terminate()
        await server.wait()


@pytest.mark.asyncio
async def test_the_relay_starts_no_worker_and_needs_a_dedicated_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that is not running answers 503 and stays stopped; without a dedicated backend nothing opens."""
    async with _runner(tmp_path) as runner:
        workers = _Workers(runner.port)
        context = _primary(tmp_path, runner, monkeypatch, workers)
        with pytest.raises(HTTPException) as stopped:
            await open_worker_relay(context.runtime_paths, _TARGET, "demo")
        assert (stopped.value.status_code, stopped.value.detail) == (503, "The agent's worker is not running.")
        assert not workers.running
        workers.running = True
        workers.handle = replace(workers.handle, status="starting")
        with pytest.raises(HTTPException) as starting:
            await open_worker_relay(context.runtime_paths, _TARGET, "demo")
        assert (starting.value.status_code, starting.value.detail) == (503, "The agent's worker is not running.")
    paths = resolve_runtime_paths(config_path=tmp_path / "c.yaml", storage_path=tmp_path / "s", process_env={})
    with pytest.raises(HTTPException) as shared:
        await open_worker_relay(paths, _TARGET, "demo")
    assert shared.value.status_code == 503
    assert "needs a dedicated worker backend" in str(shared.value.detail)


@pytest.mark.asyncio
async def test_worker_relay_takes_the_plugin_from_the_route_that_serves_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A route's relay reaches its own plugin's service; outside a plugin's route it raises."""
    async with _runner(tmp_path) as runner:
        context, _, _ = await _relay(tmp_path, runner, monkeypatch)
        app = FastAPI()
        ensure_app_state(app).api_state = config_lifecycle.ApiState(
            threading.Lock(),
            config_lifecycle.ApiSnapshot(1, context.runtime_paths, runtime_config=context.config),
        )
        route = Request({"type": "http", "app": app, PLUGIN_NAME_SCOPE_KEY: "demo"})
        assert await _get(await worker_relay(route, _TARGET), "/", service="hello") == (200, "hello", False)
        with pytest.raises(RuntimeError, match=r"worker_relay\(\) serves only a plugin's own routes"):
            await worker_relay(Request({"type": "http", "app": app}), _TARGET)


@pytest.mark.asyncio
async def test_a_stored_target_reaches_the_same_worker_after_the_primary_restarts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target the plugin stored in its state directory, loaded by the restarted primary, relays to the same service."""
    async with _runner(tmp_path) as runner:
        context, _, _ = await _relay(tmp_path, runner, monkeypatch)
        (context.state_root / "target.json").write_text(json.dumps(dump_worker_target(_TARGET)))

        # The restarted primary's backend finds the worker that kept running.
        workers = _Workers(runner.port)
        workers.running = True
        restarted = _primary(tmp_path, runner, monkeypatch, workers)
        app = FastAPI()
        ensure_app_state(app).api_state = config_lifecycle.ApiState(
            threading.Lock(),
            config_lifecycle.ApiSnapshot(1, restarted.runtime_paths, runtime_config=restarted.config),
        )
        target = load_worker_target(json.loads((restarted.state_root / "target.json").read_text()))
        relay = await worker_relay(Request({"type": "http", "app": app, PLUGIN_NAME_SCOPE_KEY: "demo"}), target)

        assert await _get(relay, "/", service="hello") == (200, "hello", False)
        assert workers.touches == 1


@pytest.mark.asyncio
async def test_worker_relay_takes_the_plugin_from_a_tools_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool's context reaches its own plugin's service, and another plugin's context never does."""
    async with _runner(tmp_path) as runner:
        context, _, _ = await _relay(tmp_path, runner, monkeypatch)
        assert await _get(await worker_relay(context, _TARGET), "/", service="hello") == (200, "hello", False)
        other = await worker_relay(replace(context, plugin_name="other"), _TARGET)
        assert (await _get(other, "/", service="hello"))[::2] == (503, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [False, True], ids=["in-memory", "stored"])
async def test_a_target_a_subagent_s_tool_resolves_reaches_the_subagent_s_own_worker_after_its_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stored: bool,
) -> None:
    """A tool's target from a subagent turn with its own worker relays there later; a normal turn's to the agent's."""
    paths, config = _demo_primary(tmp_path, "docker")
    workers = _AnyWorkers()
    monkeypatch.setattr(sandbox_proxy, "lease_primary_worker_manager", lambda *_a, **_k: nullcontext(workers))

    def tool_context(session_id: str) -> ToolRuntimeContext:
        return make_test_tool_runtime_context(
            agent_name="coder",
            target=replace(MessageTarget.resolve("!room:localhost", "$thread", None), session_id=session_id),
            requester_id="@alice:localhost",
            client=AsyncMock(),
            config=config,
            runtime_paths=paths,
            relations=make_relation_lookup(),
            conversation_reader=make_conversation_reader_mock(),
        )

    subagent = "b" * 32
    session = f"delegate:caller:coder:{subagent}"
    with dedicated_worker_placement(session, DedicatedWorkerPlacement(subagent, "large")):
        recorded = tool_context(session).resolve_worker_target()
    normal = tool_context("parent-session").resolve_worker_target()
    if stored:
        recorded, normal = (
            load_worker_target(json.loads(json.dumps(dump_worker_target(t)))) for t in (recorded, normal)
        )

    for target in (recorded, normal):
        await open_worker_relay(paths, target, "demo")
    assert workers.touched == [subagent_worker_key(_WORKER_KEY, subagent), _WORKER_KEY]
