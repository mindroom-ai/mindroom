"""Focused regressions for the worker Computer acceptance fixture."""

from __future__ import annotations

import importlib.util
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from mindroom.api import computers
from mindroom.config.main import Config
from tests.test_docker_worker_backend import _backend

if TYPE_CHECKING:
    from types import ModuleType


def _driver(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    path = Path(__file__).parents[1] / "scripts/test-worker-computer.py"
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location("computer_continuity_driver", path)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
