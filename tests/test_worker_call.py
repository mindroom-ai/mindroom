"""Build a toolkit's worker call without sending it, and reach its worker without starting it."""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from mindroom.agents import build_agent_toolkit
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ToolConfigEntry
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system import sandbox_proxy
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.agno_toolkits import toolkit_function, toolkit_worker_stand_ins
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.sandbox_proxy import tool_placement, worker_call
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from mindroom.workers.models import WorkerHandle, WorkerSpec

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.agno_toolkits import AnyToolkit
    from mindroom.tool_system.worker_routing import WorkerScope

_TOKEN = "proxy-token"  # noqa: S105
_IDENTITY = ToolExecutionIdentity("matrix", "code", "@alice:localhost", "!room:localhost", None, None, "session")


def _paths(tmp_path: Path, env: dict[str, str]) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SANDBOX_PROXY_TOKEN": _TOKEN, "MY_SERVICE_URL": "http://service.example", **env},
    )


def _shell(paths: RuntimePaths, worker_scope: WorkerScope | None = None) -> AnyToolkit:
    """Build the agent's shell toolkit, with its own settings, as a reply to ``_IDENTITY`` builds it."""
    entry = ToolConfigEntry(
        name="shell",
        overrides={"extra_env_passthrough": ["MY_SERVICE_URL"], "shell_path_prepend": ["/opt/tools/bin"]},
    )
    config = Config(agents={"code": AgentConfig(display_name="Code", tools=[entry], worker_scope=worker_scope)})
    toolkit = build_agent_toolkit(
        "shell",
        agent_tools=resolve_agent_tools(config, "code", builtin_tool_registry()),
        placement=tool_placement(paths, builtin_tool_registry()),
        runtime_paths=paths,
        tool_config_overrides=config.resolve_agent("code").tools.authored[0].tool_config_overrides,
        execution_identity=_IDENTITY,
    )
    assert toolkit is not None
    return toolkit


async def _run_shell_command(toolkit: AnyToolkit, command: str) -> object:
    function = toolkit_function(toolkit, "run_shell_command")
    assert function is not None
    return await function.entrypoint(command)


@pytest.fixture
def executed(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Answer every call a worker or runner is sent, and keep each request."""
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=json.dumps({"ok": True, "result": "ran"}).encode() + b"\n")

    client = httpx.Client
    monkeypatch.setattr(
        sandbox_proxy.httpx,
        "Client",
        lambda **kwargs: client(transport=httpx.MockTransport(answer), **kwargs),
    )
    return requests


class _DedicatedWorkers:
    """A dedicated backend's workers: an ensure starts one, a touch only finds it, and a stopped one is idle."""

    cleanup_locator = None

    def __init__(self) -> None:
        self.workers: dict[str, WorkerHandle] = {}
        self.started: list[str] = []

    def ensure_worker(self, spec: WorkerSpec, *, progress_sink: object = None) -> WorkerHandle:
        del progress_sink
        self.started.append(spec.worker_key)
        self.workers[spec.worker_key] = WorkerHandle(
            worker_id="worker-1",
            worker_key=spec.worker_key,
            endpoint="http://worker-1:8766/api/sandbox-runner/execute",
            auth_token="worker-token",  # noqa: S106
            status="ready",
            backend_name="kubernetes",
            last_used_at=0.0,
            created_at=0.0,
            debug_metadata={"api_root": "http://worker-1:8766/api/sandbox-runner"},
        )
        return self.workers[spec.worker_key]

    def touch_worker(self, worker_key: str) -> WorkerHandle | None:
        return self.workers.get(worker_key)

    def stop(self) -> None:
        self.workers = {key: replace(handle, status="idle") for key, handle in self.workers.items()}


@pytest.fixture
def dedicated(monkeypatch: pytest.MonkeyPatch) -> _DedicatedWorkers:
    """Route every worker call to these dedicated workers."""
    workers = _DedicatedWorkers()
    monkeypatch.setattr(sandbox_proxy, "lease_primary_worker_manager", lambda *_args, **_kwargs: nullcontext(workers))
    return workers


def _unsent(sent: httpx.Request) -> dict[str, Any]:
    """Return what a sent call carried, less its arguments and what each sending adds."""
    return {**json.loads(sent.content), "args": [], "kwargs": {}, "request_id": None}


@pytest.mark.asyncio
@pytest.mark.usefixtures("current_static_runner")
@pytest.mark.parametrize("worker_scope", [None, "user"])
async def test_a_call_built_unsent_is_the_one_its_toolkit_sends_to_the_shared_runner(
    tmp_path: Path,
    executed: list[httpx.Request],
    worker_scope: WorkerScope | None,
) -> None:
    """A toolkit's unsent call is built exactly as its sent calls are, and the shared runner needs no start."""
    paths = _paths(
        tmp_path,
        {
            "MINDROOM_SANDBOX_PROXY_URL": "http://runner:8765",
            "MINDROOM_SANDBOX_EXECUTION_MODE": "selective",
            "MINDROOM_SANDBOX_PROXY_TOOLS": "shell",
        },
    )
    toolkit = _shell(paths, worker_scope)
    stand_ins = toolkit_worker_stand_ins(toolkit)
    assert stand_ins is not None

    looked_up = worker_call(stand_ins, "run_shell_command", start=False)
    assert await _run_shell_command(toolkit, "echo hi") == "ran"
    [sent] = executed
    call = worker_call(stand_ins, "run_shell_command")

    assert call == looked_up
    assert call is not None
    assert call.request.model_dump(mode="json") == _unsent(sent)
    assert call.request.extra_env_passthrough == "MY_SERVICE_URL"
    assert call.request.execution_env["MY_SERVICE_URL"] == "http://service.example"
    assert call.request.tool_init_overrides == {"shell_path_prepend": "/opt/tools/bin"}
    assert call.request.base_dir is not None
    assert str(sent.url) == f"{call.api_root}/execute" == "http://runner:8765/api/sandbox-runner/execute"
    assert sent.headers["x-mindroom-sandbox-token"] == _TOKEN
    assert call.headers == {"x-mindroom-sandbox-token": _TOKEN}
    # Building a call sends nothing.
    assert len(executed) == 1


@pytest.mark.asyncio
async def test_a_lookup_finds_a_dedicated_worker_only_while_it_runs(
    tmp_path: Path,
    executed: list[httpx.Request],
    dedicated: _DedicatedWorkers,
) -> None:
    """Before the first call the lookup finds no worker and starts none; after it, the lookup reaches that worker."""
    paths = _paths(tmp_path, {"MINDROOM_WORKER_BACKEND": "kubernetes"})
    toolkit = _shell(paths)
    stand_ins = toolkit_worker_stand_ins(toolkit)
    assert stand_ins is not None

    assert worker_call(stand_ins, "run_shell_command", start=False) is None
    assert dedicated.started == []

    assert await _run_shell_command(toolkit, "echo hi") == "ran"
    [sent] = executed
    [worker_key] = dedicated.started
    call = worker_call(stand_ins, "run_shell_command", start=False)

    assert call is not None
    assert dedicated.started == [worker_key]
    assert call.request.worker_key == worker_key
    assert call.request.model_dump(mode="json") == _unsent(sent)
    assert str(sent.url) == f"{call.api_root}/execute" == "http://worker-1:8766/api/sandbox-runner/execute"
    assert call.headers == {"x-mindroom-sandbox-token": "worker-token"}

    dedicated.stop()
    assert worker_call(stand_ins, "run_shell_command", start=False) is None
    assert dedicated.started == [worker_key]
    assert worker_call(stand_ins, "run_shell_command") == call
    assert dedicated.started == [worker_key, worker_key]
    # Building a call sends nothing.
    assert len(executed) == 1


def test_a_toolkit_whose_calls_run_here_has_no_worker_stand_ins(tmp_path: Path) -> None:
    """Only a toolkit built to run in a worker has stand-ins to build its calls with."""
    paths = _paths(tmp_path, {"MINDROOM_SANDBOX_EXECUTION_MODE": "off"})
    assert toolkit_worker_stand_ins(_shell(paths)) is None


@pytest.mark.parametrize("start", [True, False])
def test_a_call_to_the_shared_runner_needs_its_proxy_url(tmp_path: Path, start: bool) -> None:
    """Without a proxy URL a call has no runner to go to, whether or not the lookup starts anything."""
    paths = _paths(tmp_path, {"MINDROOM_SANDBOX_EXECUTION_MODE": "all"})
    stand_ins = toolkit_worker_stand_ins(_shell(paths, "user"))
    assert stand_ins is not None

    with pytest.raises(RuntimeError, match="MINDROOM_SANDBOX_PROXY_URL must be set"):
        worker_call(stand_ins, "run_shell_command", start=start)
