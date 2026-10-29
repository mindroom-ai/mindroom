"""Which workspace one sandbox-runner request acts in, as each place that uses it sees it.

The runner resolves one workspace per request and uses it for the shell/python HOME and `.mindroom/worker-env.sh`
workspace, the execute output root, the subprocess cwd, the browser workspace, the save-attachment target and the
view-file root; worker preparation confines a client `base_dir` first.
These tests pin the answer at every one of those places for each request shape.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from mindroom.api import sandbox_runner, sandbox_worker_prep
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.bootstrap import ensure_tool_registry_loaded
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    agent_workspace_root_path,
    private_instance_scope_root_path,
    resolve_worker_key,
    worker_dir_name,
)
from mindroom.worker_computer.worker_browser import WorkerBrowserRuntime
from mindroom.workers.backends.local import local_worker_state_paths_for_root
from mindroom.workers.protocol import WORKER_PROTOCOL_VERSION, ExecuteRequest, WorkerFailure
from tests.api.test_view_file import HEADERS, TOKEN

if TYPE_CHECKING:
    import httpx

    from mindroom.constants import RuntimePaths

_AGENTS = {
    "general": AgentConfig(display_name="General", memory_backend="file"),
    "coder": AgentConfig(display_name="Coder", worker_scope="user", memory_backend="file"),
    "writer": AgentConfig(display_name="Writer", private=AgentPrivateConfig(per="user_agent", root="workspace")),
    # A shared agent without file memory, which still works in its own workspace.
    "bare": AgentConfig(display_name="Bare", memory_backend="none"),
}
_PAYLOAD = b"payload"


@pytest.fixture(autouse=True)
def _load_tools() -> None:
    ensure_tool_registry_loaded(resolve_runtime_paths(config_path=Path("config.yaml")))


@dataclass(frozen=True)
class _Shape:
    """One request shape against a runner whose filesystem matches it."""

    tmp_root: Path
    runtime_paths: RuntimePaths
    config: Config
    routing: dict[str, object]
    # Workspace names in match order; a resolved path is reported by the first name it equals.
    labels: dict[str, Path]

    def client(self) -> TestClient:
        app = FastAPI()
        sandbox_runner.initialize_sandbox_runner_app(app, self.runtime_paths, config=self.config, runner_token=TOKEN)
        app.state.worker_browser = WorkerBrowserRuntime()
        app.include_router(sandbox_runner.router)
        return TestClient(app, raise_server_exceptions=False)

    def label(self, path: object) -> str | None:
        if path is None:
            return None
        resolved = Path(str(path)).resolve()
        return next((name for name, root in self.labels.items() if resolved == root.resolve()), str(resolved))

    def execute_payload(self, tool_name: str, function_name: str) -> dict[str, object]:
        return {**self.routing, "tool_name": tool_name, "function_name": function_name, "args": [], "kwargs": {}}

    def attachment_payload(self) -> dict[str, object]:
        return {
            **self.routing,
            "attachment_id": "att_sample",
            "mindroom_output_path": "incoming/sample.txt",
            "sha256": hashlib.sha256(_PAYLOAD).hexdigest(),
            "size_bytes": len(_PAYLOAD),
            "bytes_b64": base64.b64encode(_PAYLOAD).decode("ascii"),
        }

    def view_payload(self) -> dict[str, object]:
        return {**self.routing, "path": "sample.png"}


def _identity(agent_name: str | None) -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name or "general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-1",
        tenant_id="tenant-123",
    )


def _shape(tmp_path: Path, *, scope: str | None, agent: str | None, client_base_dir: str) -> _Shape:
    """Build one request shape: unkeyed on a static runner, or keyed on its dedicated worker.

    ``client_base_dir`` is ``none``, ``absolute`` (a path outside every storage root), ``agent`` (the agent workspace the
    primary normally sends), ``scratch`` (a directory inside the worker root), or ``outside`` (another agent's
    workspace the worker does not see).
    """
    identity = _identity(agent)
    config = Config(agents=_AGENTS, models={})
    labels: dict[str, Path] = {}
    if scope is None:
        storage_root = tmp_path / "storage"
        runtime_paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=storage_root,
            process_env={"MINDROOM_SANDBOX_RUNNER_MODE": "true"},
        )
        worker_key = None
        if agent is not None:
            labels["agent"] = agent_workspace_root_path(storage_root, agent)
    else:
        worker_key = resolve_worker_key(scope, identity, agent_name=agent)
        assert worker_key is not None
        storage_root = tmp_path / "shared"
        worker_root = storage_root / "workers" / worker_dir_name(worker_key)
        # Path-only requests reuse an existing interpreter without bootstrapping packages.
        interpreter = worker_root / "venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(sys.executable)
        runtime_paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=worker_root,
            process_env={
                "MINDROOM_SANDBOX_RUNNER_MODE": "true",
                "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY": worker_key,
                "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT": str(worker_root),
                "MINDROOM_SANDBOX_SHARED_STORAGE_ROOT": str(storage_root),
            },
        )
        if agent == "writer":
            labels["agent"] = private_instance_scope_root_path(storage_root, worker_key) / "writer" / "workspace"
        elif agent is not None:
            labels["agent"] = agent_workspace_root_path(storage_root, agent)
        labels["worker_default"] = worker_root / "workspace"
        labels["worker_root"] = worker_root
    raw_base_dir = {
        "none": None,
        "absolute": str(tmp_path / "static-workspace"),
        "agent": labels["agent"].relative_to(storage_root).as_posix() if "agent" in labels else None,
        "scratch": str(labels.get("worker_default", tmp_path) / "scratch"),
        "outside": "agents/other/workspace",
    }[client_base_dir]
    if raw_base_dir is not None and client_base_dir != "agent":
        labels["client"] = storage_root / raw_base_dir
    labels["runner_cwd"] = Path.cwd()
    labels["runner_home"] = Path(os.environ["HOME"])
    routing: dict[str, object] = {
        "protocol": WORKER_PROTOCOL_VERSION,
        "base_dir": raw_base_dir,
        "worker_key": worker_key,
        "worker_scope": scope,
        "routing_agent_name": agent,
        "execution_identity": asdict(identity),
        "private_agent_names": ["writer"],
    }
    return _Shape(
        tmp_root=tmp_path,
        runtime_paths=runtime_paths,
        config=config,
        routing=routing,
        labels=labels,
    )


def _response_line(response: httpx.Response) -> dict[str, object]:
    """Return the final result line of a possibly streamed execute response."""
    return json.loads(response.text.strip().splitlines()[-1])


def _resolve_every_endpoint(shape: _Shape, monkeypatch: pytest.MonkeyPatch) -> dict[str, str | None]:
    """Return the workspace label, or failure, each resolver picks for one request shape."""
    resolved: dict[str, str | None] = {}

    # Execute shell: HOME and the worker-env.sh workspace, the output root, the subprocess cwd.
    request = ExecuteRequest(**shape.execute_payload("shell", "run_shell_command"))
    route = sandbox_runner._prepare_route_with_config(
        request,
        runtime_paths=shape.runtime_paths,
        config=shape.config,
        runner_token=TOKEN,
    )
    assert not isinstance(route, WorkerFailure), route
    context = sandbox_runner._prepare_execute_request(sandbox_runner._ExecuteCall(request=request, route=route))
    resolved["home"] = shape.label(context.execution_env.get("HOME"))
    resolved["hook_workspace"] = shape.label(context.execution_env.get("MINDROOM_AGENT_WORKSPACE"))
    resolved["subprocess_cwd"] = shape.label(sandbox_runner._prepare_subprocess_context(context).subprocess_cwd)
    resolved["output_root"] = shape.label(context.request.tool_output_workspace_root)

    client = shape.client()
    saved = client.post("/api/sandbox-runner/save-attachment", headers=HEADERS, json=shape.attachment_payload())
    assert saved.status_code == 200, saved.text
    if saved.json()["ok"]:
        written = [path.parent.parent for path in shape.tmp_root.rglob("sample.txt") if path.parent.name == "incoming"]
        assert len(written) == 1, written
        resolved["save_attachment"] = shape.label(written[0])
    else:
        resolved["save_attachment"] = f"fail:{saved.json()['failure_kind']}"

    viewed_roots: list[Path] = []

    def capture_view(_path: str, workspace: Path, _file_access: object) -> dict[str, object]:
        viewed_roots.append(workspace)
        return {}

    monkeypatch.setattr(sandbox_runner, "_view_file_result_envelope", capture_view)
    viewed = client.post("/api/sandbox-runner/view-file", headers=HEADERS, json=shape.view_payload())
    assert viewed.status_code == 200, viewed.text
    if viewed.json()["ok"]:
        resolved["view_file"] = shape.label(viewed_roots[0])
    else:
        resolved["view_file"] = f"fail:{viewed.json()['failure_kind']}"

    browser_roots: list[object] = []

    def capture_browser(**kwargs: object) -> object:
        browser_roots.append(kwargs["tool_output_workspace_root"])
        raise HTTPException(status_code=418, detail="captured")

    # The browser tool is never built; only the workspace it would be bound to is recorded.
    monkeypatch.setattr(sandbox_runner, "_resolve_entrypoint", capture_browser)
    browsed = client.post(
        "/api/sandbox-runner/execute",
        headers=HEADERS,
        json=shape.execute_payload("browser", "browser_control"),
    )
    assert browsed.status_code == 200, browsed.text
    if browser_roots:
        resolved["browser"] = shape.label(browser_roots[0])
    else:
        resolved["browser"] = f"fail:{browsed.json()['failure_kind']}"
    return resolved


_COLUMNS = (
    "home",
    "hook_workspace",
    "subprocess_cwd",
    "output_root",
    "save_attachment",
    "view_file",
    "browser",
)


def _row(*values: str | None) -> dict[str, str | None]:
    return dict(zip(_COLUMNS, values, strict=True))


@pytest.mark.parametrize(
    ("scope", "agent", "client_base_dir", "expected"),
    [
        # Unkeyed static-runner request with a routing agent acts in that agent's workspace; the browser needs a
        # dedicated worker.
        pytest.param(
            None,
            "general",
            "none",
            _row("agent", "agent", "agent", "agent", "agent", "agent", "fail:tool"),
            id="unkeyed-agent",
        ),
        # Unkeyed request without an agent acts in its absolute base_dir.
        pytest.param(
            None,
            None,
            "absolute",
            _row("client", "client", "client", "client", "client", "client", "fail:tool"),
            id="unkeyed-absolute-base-dir",
        ),
        # An agent without file memory still has a workspace, so an unkeyed request routed to it acts there.
        pytest.param(
            None,
            "bare",
            "none",
            _row("agent", "agent", "agent", "agent", "agent", "agent", "fail:tool"),
            id="unkeyed-agent-without-file-memory",
        ),
        # Keyed dedicated shared-scope worker: without a base_dir the routing agent's workspace.
        pytest.param(
            "shared",
            "general",
            "none",
            _row("agent", "agent", "agent", "agent", "agent", "agent", "agent"),
            id="shared-no-base-dir",
        ),
        pytest.param(
            "shared",
            "general",
            "agent",
            _row("agent", "agent", "agent", "agent", "agent", "agent", "agent"),
            id="shared-agent-base-dir",
        ),
        # A keyed base_dir inside the worker root wins over the routing agent's workspace.
        pytest.param(
            "shared",
            "general",
            "scratch",
            _row("client", "client", "client", "client", "client", "client", "client"),
            id="shared-scratch-base-dir",
        ),
        # A keyed shared worker routed to an agent without file memory acts in that agent's workspace.
        pytest.param(
            "shared",
            "bare",
            "none",
            _row("agent", "agent", "agent", "agent", "agent", "agent", "agent"),
            id="shared-agent-without-file-memory",
        ),
        # Keyed user-scope worker routed to a user-scope agent.
        pytest.param(
            "user",
            "coder",
            "none",
            _row("agent", "agent", "agent", "agent", "agent", "agent", "agent"),
            id="user-no-base-dir",
        ),
        pytest.param(
            "user",
            "coder",
            "agent",
            _row("agent", "agent", "agent", "agent", "agent", "agent", "agent"),
            id="user-agent-base-dir",
        ),
        pytest.param(
            "user",
            "coder",
            "scratch",
            _row("client", "client", "client", "client", "client", "client", "client"),
            id="user-scratch-base-dir",
        ),
        # Keyed user_agent worker of a private agent.
        pytest.param(
            "user_agent",
            "writer",
            "none",
            _row("agent", "agent", "agent", "agent", "agent", "agent", "agent"),
            id="user-agent-no-base-dir",
        ),
        pytest.param(
            "user_agent",
            "writer",
            "agent",
            _row("agent", "agent", "agent", "agent", "agent", "agent", "agent"),
            id="user-agent-agent-base-dir",
        ),
        pytest.param(
            "user_agent",
            "writer",
            "scratch",
            _row("client", "client", "client", "client", "client", "client", "client"),
            id="user-agent-scratch-base-dir",
        ),
    ],
)
def test_each_resolver_picks_the_pinned_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str | None,
    agent: str | None,
    client_base_dir: str,
    expected: dict[str, str | None],
) -> None:
    """Each request shape resolves to the pinned workspace, or failure, at every runner resolver."""
    shape = _shape(tmp_path, scope=scope, agent=agent, client_base_dir=client_base_dir)

    assert _resolve_every_endpoint(shape, monkeypatch) == expected


@pytest.mark.parametrize(("scope", "agent"), [(None, "general"), ("shared", "general"), ("user_agent", "writer")])
def test_a_tool_without_a_base_dir_runs_in_the_agent_workspace(tmp_path: Path, scope: str | None, agent: str) -> None:
    """A tool that declares no base_dir and gets no workspace env still runs its subprocess in the agent's workspace."""
    shape = _shape(tmp_path, scope=scope, agent=agent, client_base_dir="none")
    request = ExecuteRequest(**shape.execute_payload("calculator", "add"))
    route = sandbox_runner._prepare_route_with_config(
        request,
        runtime_paths=shape.runtime_paths,
        config=shape.config,
        runner_token=TOKEN,
    )
    assert not isinstance(route, WorkerFailure), route
    context = sandbox_runner._prepare_execute_request(sandbox_runner._ExecuteCall(request=request, route=route))

    assert "MINDROOM_AGENT_WORKSPACE" not in context.execution_env
    assert shape.label(sandbox_runner._prepare_subprocess_context(context).subprocess_cwd) == "agent"


@pytest.mark.parametrize("client_base_dir", ["outside", "absolute"])
@pytest.mark.parametrize("route", ["execute", "view-file", "save-attachment", "browser"])
def test_keyed_base_dir_outside_visible_roots_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client_base_dir: str,
    route: str,
) -> None:
    """A keyed base_dir outside the worker root and its visible workspaces is refused, never replaced."""
    shape = _shape(tmp_path, scope="shared", agent="general", client_base_dir=client_base_dir)
    payload = {
        "execute": shape.execute_payload("shell", "run_shell_command"),
        "view-file": shape.view_payload(),
        "save-attachment": shape.attachment_payload(),
        "browser": shape.execute_payload("browser", "browser_control"),
    }[route]
    endpoint = "execute" if route == "browser" else route
    built: list[object] = []
    monkeypatch.setattr(sandbox_runner, "_resolve_entrypoint", lambda **kwargs: built.append(kwargs))

    response = shape.client().post(f"/api/sandbox-runner/{endpoint}", headers=HEADERS, json=payload)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": False,
        "error": response.json()["error"],
        "failure_kind": "tool",
    }
    assert "base_dir must stay inside a visible workspace or the worker root" in response.json()["error"]
    assert built == []
    assert not list(tmp_path.rglob("sample.txt"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
@pytest.mark.parametrize("route", ["execute", "browser", "save-attachment", "view-file"])
@pytest.mark.parametrize("scope", ["shared", "user_agent"])
def test_unmounted_agent_workspace_is_a_tool_failure(tmp_path: Path, route: str, scope: str) -> None:
    """An agent workspace the dedicated worker cannot create fails the call, not the worker, at every endpoint."""
    agent = "writer" if scope == "user_agent" else "general"
    shape = _shape(tmp_path, scope=scope, agent=agent, client_base_dir="none")
    # Only a read-only parent lies where the unmounted workspace belongs.
    unmounted_parent = shape.labels["agent"].parent
    unmounted_parent.mkdir(parents=True)
    unmounted_parent.chmod(0o555)
    endpoint, payload = {
        "save-attachment": ("save-attachment", shape.attachment_payload()),
        "view-file": ("view-file", shape.view_payload()),
        "browser": ("execute", shape.execute_payload("browser", "browser_control")),
        "execute": ("execute", shape.execute_payload("shell", "run_shell_command")),
    }[route]
    try:
        response = shape.client().post(f"/api/sandbox-runner/{endpoint}", headers=HEADERS, json=payload)
    finally:
        unmounted_parent.chmod(0o755)

    assert response.status_code == 200, response.text
    result = _response_line(response)
    assert result["ok"] is False
    assert result["failure_kind"] == "tool"
    assert str(result["error"]).startswith("Agent workspace is not mounted in this worker yet"), result


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (None, "worker_default"),
        ("agents/general/workspace", "visible_root"),
        ("agents/general/workspace/sub", "visible_sub"),
        ("visible_root_absolute", "visible_root"),
        ("worker_scratch_absolute", "worker_scratch"),
        ("agents/other/workspace", "ValueError"),
        ("agents/general/workspace/../../other/workspace", "ValueError"),
        ("outside_absolute", "ValueError"),
        (42, "TypeError"),
        ("no_visible_roots", "ValueError"),
        ("linked_visible_root", "ValueError"),
    ],
)
def test_worker_base_dir_confinement(tmp_path: Path, requested: object, expected: str) -> None:
    """A client base_dir resolves inside the worker root or one unlinked visible workspace, or is refused."""
    storage_root = tmp_path / "shared"
    worker_key = "v1:tenant-123:shared:general"
    paths = local_worker_state_paths_for_root(storage_root / "workers" / worker_dir_name(worker_key))
    visible_root = agent_workspace_root_path(storage_root, "general")
    labels = {
        "worker_default": paths.workspace,
        "visible_root": visible_root,
        "visible_sub": visible_root / "sub",
        "worker_scratch": paths.root / "workspace" / "scratch",
    }
    workspace_roots: tuple[Path, ...] = (visible_root,)
    aliases = {
        "visible_root_absolute": str(visible_root),
        "worker_scratch_absolute": str(labels["worker_scratch"]),
        "outside_absolute": str(tmp_path / "elsewhere"),
        "no_visible_roots": "agents/general/workspace",
        "linked_visible_root": "agents/general/workspace",
    }
    raw = aliases.get(requested, requested) if isinstance(requested, str) else requested
    if requested == "no_visible_roots":
        workspace_roots = ()
    if requested == "linked_visible_root":
        (tmp_path / "elsewhere").mkdir()
        visible_root.parent.mkdir(parents=True)
        visible_root.symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    try:
        resolved = sandbox_worker_prep._resolve_worker_base_dir(paths, storage_root, worker_key, raw, workspace_roots)
    except (TypeError, ValueError) as exc:
        outcome = type(exc).__name__
    else:
        outcome = next((name for name, path in labels.items() if resolved == path.resolve()), str(resolved))

    assert outcome == expected
