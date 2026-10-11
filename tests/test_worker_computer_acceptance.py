"""Acceptance helpers preserve native results and wait for viewer readiness."""

from __future__ import annotations

import importlib.util
import sys
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from agno.tools.function import ToolResult

from mindroom.api import computers
from mindroom.config.main import Config
from mindroom.tool_system.media_transport import encode_media_result
from mindroom.workers.backend import WorkerBackendError
from tests.test_docker_worker_backend import _backend


def _helper() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts/testing/worker_computer_native.py"
    spec = importlib.util.spec_from_file_location("worker_computer_native", path)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_viewer_waits_for_async_module_before_connecting() -> None:
    """Page load can precede noVNC's top-level asynchronous import completion."""
    module = _helper()
    page = AsyncMock()
    await module.connect_viewer(page, {"session_id": "owned"})
    assert page.mock_calls[0].args == ("typeof window.connectComputer === 'function'",)
    assert page.mock_calls[0].kwargs == {"timeout": 30000}
    assert "connectComputer" in page.mock_calls[1].args[0]


def test_native_result_parser_uses_only_result_section() -> None:
    """Code, snapshots and braces outside the result cannot corrupt JSON readback."""
    module = _helper()
    text = '### Result\n{"value": "typed"}\n### Ran Playwright code\n```js\n() => {}\n```'
    assert module.native_json(text) == {"value": "typed"}


def test_native_result_parser_accepts_void_page_actions() -> None:
    """Native focus and storage mutations report JavaScript undefined explicitly."""
    assert _helper().native_json("### Result\nundefined\n### Ran Playwright code\n...") is None


def test_shell_readback_removes_only_known_cwd_header() -> None:
    """Native absolute upload paths use command stdout, not the display header."""
    assert _helper().shell_stdout("[cwd: /workspace]\n/workspace\n") == "/workspace\n"


def test_native_tab_indices_remain_current() -> None:
    """The native server exposes mutable indices, not stable CDP targets."""
    module = _helper()
    text = "### Result\n- 0: (current) [Fixture](http://127.0.0.1:8767/)\n- 1: [New Tab](chrome://newtab/)"
    assert module.native_tabs(text) == [
        {"index": 0, "current": True, "title": "Fixture", "url": "http://127.0.0.1:8767/"},
        {"index": 1, "current": False, "title": "New Tab", "url": "chrome://newtab/"},
    ]


def test_docker_fixture_preserves_explicit_daemon_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """An isolated runtime environment must still reach its owned Docker daemon."""
    monkeypatch.setenv("DOCKER_HOST", "tcp://127.0.0.1:2375")
    monkeypatch.setenv("UNRELATED_SECRET", "not-forwarded")
    assert _helper().docker_environment() == {"DOCKER_HOST": "tcp://127.0.0.1:2375"}


@pytest.mark.asyncio
async def test_native_call_accepts_native_function_argument(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The evaluate schema's function argument must not collide with helper routing."""
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("computer_acceptance_driver", scripts / "test-worker-computer.py")
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = object.__new__(module.Fixture)
    fixture.args = SimpleNamespace(output=tmp_path)
    fixture.execute = AsyncMock(
        return_value={"ok": True, "result": encode_media_result(ToolResult(content="### Result\n1"))},
    )
    result = await fixture.native("browser_evaluate", function="()=>1")
    fixture.execute.assert_awaited_once_with("browser_mcp", "browser_evaluate", {"function": "()=>1"})
    assert result.content == "### Result\n1"


def _driver(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    path = Path(__file__).parents[1] / "scripts/test-worker-computer.py"
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location("computer_continuity_driver", path)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("host_uid", [501, 1000, 1001])
def test_fixture_security_uses_host_worker_uid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_uid: int,
) -> None:
    """Non-root hosts retain bind-mount ownership without assuming UID 1000."""
    module = _driver(monkeypatch)
    monkeypatch.setattr("mindroom.workers.backends.docker_config.os.getuid", lambda: host_uid)
    monkeypatch.setattr("mindroom.workers.backends.docker_config.os.getgid", lambda: 100)
    args = SimpleNamespace(output=tmp_path, provider="browser", matrix_fixture=None, chat_origin=None, image="fixture")

    fixture = module.Fixture(args, "http://127.0.0.1:8765")

    assert fixture.worker_uid == host_uid


def test_fixture_security_rejects_root_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The acceptance fixture must not silently drop its non-root requirement."""
    module = _driver(monkeypatch)
    monkeypatch.setattr("mindroom.workers.backends.docker_config.os.getuid", lambda: 0)
    args = SimpleNamespace(output=tmp_path, provider="browser", matrix_fixture=None, chat_origin=None, image="fixture")

    with pytest.raises(WorkerBackendError, match="non-root"):
        module.Fixture(args, "http://127.0.0.1:8765")


@pytest.mark.asyncio
async def test_context_readback_uses_shared_agent_workspace_not_worker_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolve context from shared agent storage despite scratch and local decoys."""
    module = _driver(monkeypatch)
    shared = tmp_path / "shared"
    local = tmp_path / "local-worker"
    scratch = local / "scratch"
    canonical = shared / "agents/writer/workspace/AGENTS.md"
    local_decoy = local / "agents/writer/workspace/AGENTS.md"
    for path in (canonical, local_decoy, scratch / "AGENTS.md"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("decoy")
    canonical.write_text("Updated browser fixture context\n")
    config = tmp_path / "config.yaml"
    config.write_text("{}\n")
    monkeypatch.setenv("MINDROOM_CONFIG_PATH", str(config))
    monkeypatch.setenv("MINDROOM_STORAGE_PATH", str(local))
    monkeypatch.setenv("MINDROOM_SANDBOX_SHARED_STORAGE_ROOT", str(shared))
    monkeypatch.chdir(scratch)

    resolved = await module.command(sys.executable, "-c", module.WORKER_CONTEXT_PATH_SCRIPT, "AGENTS.md")

    assert Path(resolved) == canonical
    assert Path(resolved).read_text() == "Updated browser fixture context\n"
    assert (scratch / "AGENTS.md").read_text() == "decoy"
    assert local_decoy.read_text() == "decoy"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["history", "context", "config"])
async def test_acceptance_reconciles_config_before_browser_continuity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """Mounted edits keep the worker, while genuine config drift replaces it."""
    module = _driver(monkeypatch)
    workspace = tmp_path / "agents/writer/workspace"
    workspace.mkdir(parents=True)
    history = workspace / "history.yaml"
    context = workspace / "AGENTS.md"
    history.write_text("initial history")
    context.write_text("initial context")
    config_text = yaml.safe_dump(
        {
            "agents": {
                "writer": {
                    "display_name": "Writer",
                    "worker_scope": "user_agent",
                    "knowledge_bases": ["history"],
                    "context_files": ["AGENTS.md"],
                },
            },
            "knowledge_bases": {"history": {"path": str(history)}},
        },
    )
    backend, client, _sync_calls = _backend(monkeypatch, tmp_path, config_text=config_text)
    monkeypatch.setattr(
        computers,
        "lease_configured_primary_worker_manager",
        lambda *_args, **_kwargs: nullcontext(backend),
    )
    fixture = object.__new__(module.Fixture)
    fixture.config = Config.model_validate(yaml.safe_load(config_text))
    fixture.paths = backend._runtime_paths
    fixture.viewer = "@alice:example.org"
    fixture.room = "!room:example.org"
    fixture.agent = "@writer:example.org"
    first = backend.ensure_worker(fixture.target(fixture.viewer).spec)
    container = client.containers.created_containers[0]

    if change == "config":
        updated = yaml.safe_load(config_text)
        updated["agents"]["writer"]["display_name"] = "Updated writer"
        (tmp_path / "config.yaml").write_text(yaml.safe_dump(updated))
    else:
        (history if change == "history" else context).write_text("updated conversation")

    second = await fixture.reconcile_worker()

    if change == "config":
        assert container.status == "removed"
        assert len(client.containers.created_containers) == 2
    else:
        assert second.worker_id == first.worker_id
        assert second.endpoint == first.endpoint
        assert container.status == "running"
        assert len(client.containers.created_containers) == 1


@pytest.mark.parametrize("query", ["", "?after-chat=1"])
def test_native_fixture_tab_matching_keeps_exact_origin_and_path(
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    """Continuity navigation keeps the fixture discoverable among unrelated tabs."""
    module = _driver(monkeypatch)
    tabs = [
        {"index": 0, "url": "https://127.0.0.1:8767/"},
        {"index": 1, "url": "http://localhost:8767/"},
        {"index": 2, "url": "http://127.0.0.1:8768/"},
        {"index": 3, "url": "http://127.0.0.1:8767/other"},
        {"index": 4, "url": "chrome://newtab/"},
        {"index": 5, "url": "http://127.0.0.1:8767/" + query},
    ]
    assert module.fixture_tab_index(tabs) == 5
