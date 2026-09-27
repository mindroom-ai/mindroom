"""Startup retirement of workers that mounted whole state roots, and reports of what they may have left."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

from mindroom.constants import resolve_primary_runtime_paths
from mindroom.tool_system.worker_routing import private_instance_scope_root_path
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends.legacy_state_root_mounts import (
    _report_links_left_by_state_root_mounts,
    retire_state_root_worker_mounts,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_ATTACKER_KEY = "v1:default:user_agent:~@mallory:localhost:mind"
_VICTIM_KEY = "v1:default:user_agent:~@alice:localhost:mind"


def _runtime_paths(tmp_path: Path, backend: str | None = None) -> RuntimePaths:
    (tmp_path / "config.yaml").write_text(
        "agents:\n  mind:\n    display_name: Mind\n    private:\n      per: user_agent\n      root: notes\n",
        encoding="utf-8",
    )
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={} if backend is None else {"MINDROOM_WORKER_BACKEND": backend},
    )


def _warnings(logs: list[dict[str, object]]) -> dict[str, list[str]]:
    return {str(entry["event"]): list(entry.get("paths", [])) for entry in logs if entry["log_level"] == "warning"}


def test_report_warns_about_links_above_workspaces_and_hard_links_leaving_them(tmp_path: Path) -> None:
    """Planted links and escaping hard links are reported by path; workspace-internal ones and aliases are not."""
    runtime_paths = _runtime_paths(tmp_path)
    storage = runtime_paths.storage_root
    attacker = private_instance_scope_root_path(storage, _ATTACKER_KEY) / "mind"
    victim = private_instance_scope_root_path(storage, _VICTIM_KEY) / "mind"
    for root in (attacker, victim):
        (root / "notes").mkdir(parents=True)
        (root / "sessions").mkdir()
    victim_db = victim / "sessions" / "mind.db"
    victim_db.write_bytes(b"victim session")
    shared = storage / "agents" / "helper"
    (shared / "workspace" / "docs").mkdir(parents=True)
    (shared / "workspace" / "docs" / "a.md").write_text("a", encoding="utf-8")
    # The worker's own links and hard links inside its workspace are normal.
    (shared / "workspace" / "latest.md").symlink_to("docs/a.md")
    os.link(shared / "workspace" / "docs" / "a.md", shared / "workspace" / "docs" / "b.md")
    # Old wide mounts let a worker plant these above its workspace and keep a victim file.
    (shared / "sessions").symlink_to(victim / "sessions", target_is_directory=True)
    (attacker / "learning").symlink_to(victim, target_is_directory=True)
    os.link(victim_db, attacker / "notes" / "kept.db")
    # A verified legacy alias at the namespace level is the primary's own link.
    (storage / "private_instances" / "alias").symlink_to(attacker.parent.name, target_is_directory=True)
    before = sorted((path, path.is_symlink()) for path in storage.rglob("*"))

    with capture_logs() as logs:
        _report_links_left_by_state_root_mounts(runtime_paths)

    warnings = _warnings(logs)
    link_paths = next(paths for event, paths in warnings.items() if event.startswith("Links above"))
    hard_link_paths = next(paths for event, paths in warnings.items() if event.startswith("Hard links"))
    assert sorted(link_paths) == [
        "agents/helper/sessions",
        f"private_instances/{attacker.parent.name}/mind/learning",
    ]
    assert sorted(hard_link_paths) == sorted(
        [
            f"private_instances/{attacker.parent.name}/mind/notes/kept.db",
            f"private_instances/{victim.parent.name}/mind/sessions/mind.db",
        ],
    )
    assert sorted((path, path.is_symlink()) for path in storage.rglob("*")) == before
    assert victim_db.read_bytes() == b"victim session"


def test_report_is_silent_for_a_clean_layout(tmp_path: Path) -> None:
    """Storage without planted entries produces no warning."""
    runtime_paths = _runtime_paths(tmp_path)
    (runtime_paths.storage_root / "agents" / "helper" / "workspace").mkdir(parents=True)
    (private_instance_scope_root_path(runtime_paths.storage_root, _VICTIM_KEY) / "mind" / "notes").mkdir(parents=True)

    with capture_logs() as logs:
        _report_links_left_by_state_root_mounts(runtime_paths)

    assert _warnings(logs) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["docker", "kubernetes"])
async def test_startup_stops_old_workers_through_their_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    """Dedicated backends stop old workers before the scan; a failing API is logged and never blocks startup."""
    runtime_paths = _runtime_paths(tmp_path, backend)
    calls: list[RuntimePaths] = []
    target = (
        "mindroom.workers.backends.docker.remove_docker_workers_mounting_state_roots"
        if backend == "docker"
        else "mindroom.workers.backends.kubernetes.stop_kubernetes_workers_mounting_state_roots"
    )
    monkeypatch.setattr(target, lambda paths: calls.append(paths) or ("old-worker",))
    scans: list[RuntimePaths] = []
    monkeypatch.setattr(
        "mindroom.workers.backends.legacy_state_root_mounts._report_links_left_by_state_root_mounts",
        scans.append,
    )

    with capture_logs() as logs:
        await retire_state_root_worker_mounts(runtime_paths)

    assert calls == [runtime_paths]
    assert scans == [runtime_paths]
    assert any(entry["event"].startswith("Stopped sandbox workers") for entry in logs)

    def unavailable(_paths: RuntimePaths) -> tuple[str, ...]:
        message = "cluster unavailable"
        raise WorkerBackendError(message)

    monkeypatch.setattr(target, unavailable)
    with capture_logs() as logs:
        await retire_state_root_worker_mounts(runtime_paths)

    assert scans == [runtime_paths, runtime_paths]
    assert any(entry["log_level"] == "error" for entry in logs)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [None, "static"])
async def test_startup_skips_backends_without_dedicated_workers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str | None,
) -> None:
    """Only Docker and Kubernetes ever mounted state roots into workers."""
    scans: list[RuntimePaths] = []
    monkeypatch.setattr(
        "mindroom.workers.backends.legacy_state_root_mounts._report_links_left_by_state_root_mounts",
        scans.append,
    )

    await retire_state_root_worker_mounts(_runtime_paths(tmp_path, backend))

    assert scans == []
