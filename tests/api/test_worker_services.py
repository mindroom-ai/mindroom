"""A dedicated worker's runner starts, reports, and stops the services plugins run in it."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import socket
import sys
import threading
from contextlib import asynccontextmanager, nullcontext, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
import pytest
import uvicorn

from mindroom import plugin_worker_services
from mindroom.api import sandbox_runner_app
from mindroom.api import worker_services as worker_services_module
from mindroom.api.sandbox_runner import initialize_sandbox_runner_app
from mindroom.api.worker_services import _services, close_worker_services
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.plugin_context import build_plugin_context
from mindroom.plugin_lifecycle import PluginLifespans
from mindroom.plugin_worker_services import (
    WorkerService,
    WorkerServiceError,
    ensure_worker_service,
    stop_worker_service,
)
from mindroom.tool_system import sandbox_proxy
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target, subagent_worker_key
from mindroom.workers.compatibility import WORKER_PROTOCOL_VERSION
from mindroom.workers.models import WorkerHandle, WorkerSpec
from mindroom.workers.placements import DedicatedWorkerPlacement, dedicated_worker_placement
from tests.conftest import bind_runtime_paths, publish_test_plugins

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from mindroom.constants import RuntimePaths
    from mindroom.plugin_context import PluginContext

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Services run in their own process groups.")

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
        services = _services(sandbox_runner_app.app)
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
        assert _services(sandbox_runner_app.app).instance("demo", "hello") is None


@pytest.mark.asyncio
async def test_a_crashed_service_is_reported_and_not_restarted(tmp_path: Path) -> None:
    """A program that exits is reported with its exit code, and ensuring it again starts nothing until a stop."""
    async with _runner(tmp_path) as runner:
        _, state = await _call("POST", runner.url("crash"), _body("crash.py"), _AUTH)
        assert state["state"] == "exited"
        assert state["exit_code"] == 3
        process = _services(sandbox_runner_app.app).instance("demo", "crash").process  # type: ignore[union-attr]
        _, again = await _call("POST", runner.url("crash"), _body("crash.py"), _AUTH)
        assert again == state
        assert _services(sandbox_runner_app.app).instance("demo", "crash").process is process  # type: ignore[union-attr]


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
    """A plugin this worker lacks, a program outside the plugin, another protocol, no token: refusals, no process."""
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
        assert _services(sandbox_runner_app.app).instance("demo", "hello") is None
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

    def ensure_worker(self, spec: WorkerSpec, *, progress_sink: object = None) -> WorkerHandle:
        del progress_sink
        assert spec.worker_key == _WORKER_KEY
        self.running = True
        return self.handle

    def touch_worker(self, worker_key: str) -> WorkerHandle | None:
        assert worker_key == _WORKER_KEY
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
        instance = _services(sandbox_runner_app.app).instance("demo", "hello")
        assert instance is not None
        assert instance.port == state.port
        assert instance.idle_timeout_seconds == 300

        await stop_worker_service(context, _SERVICE, _TARGET)
        assert _services(sandbox_runner_app.app).instance("demo", "hello") is None
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
        services = _services(sandbox_runner_app.app)

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
    """A name the runner's routes cannot carry, or a timeout that is not positive, fails where the plugin declares it."""
    with pytest.raises(ValueError, match=r"(?i)worker service"):
        WorkerService(name, "hello.py", idle_timeout_seconds=idle)


@pytest.mark.asyncio
async def test_a_service_that_fails_to_stop_holds_up_no_other(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plugin load or shutdown stops every started service, even when one worker cannot be reached."""
    stopped: list[str] = []

    async def stop(_paths: object, _worker_key: str, _plugin: str, name: str) -> None:
        stopped.append(name)
        if name == "broken":
            msg = "worker gone"
            raise WorkerServiceError(msg)

    monkeypatch.setattr(plugin_worker_services, "_stop_recorded", stop)
    for name in ("broken", "hello"):
        plugin_worker_services._started[(_WORKER_KEY, "demo", name)] = None  # type: ignore[assignment]
    await plugin_worker_services.stop_started_worker_services()
    assert stopped == ["broken", "hello"]
    assert plugin_worker_services._started == {}


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
        assert _services(sandbox_runner_app.app).instance("demo", "hello") is None


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
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MINDROOM_WORKER_BACKEND": "docker"},
    )
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text('{"name": "demo"}')
    config = _config(plugin_root, paths)
    publish_test_plugins(config, paths)
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
