"""Tests for primary-runtime worker validation snapshot caching."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.tool_system.metadata import ToolValidationInfo
from mindroom.workers import runtime as workers_runtime_module

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _clear_worker_validation_snapshot_cache() -> Iterator[None]:
    """Isolate the process-local worker validation snapshot cache."""
    workers_runtime_module.clear_worker_validation_snapshot_cache()
    yield
    workers_runtime_module.clear_worker_validation_snapshot_cache()


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Return a runtime path set rooted under one pytest temp directory."""
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )


def test_serialized_kubernetes_worker_validation_snapshot_reuses_cached_resolver_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Repeated snapshot requests for one config should invoke the resolver once."""
    runtime_paths = _runtime_paths(tmp_path)
    runtime_config = Config()
    calls: list[Config] = []

    def fake_resolver(*_args: object, **_kwargs: object) -> dict[str, ToolValidationInfo]:
        calls.append(runtime_config)
        return {"fake": ToolValidationInfo(name="fake")}

    monkeypatch.setattr(
        "mindroom.tool_system.catalog.resolved_tool_validation_snapshot_for_runtime",
        fake_resolver,
    )

    first_snapshot = workers_runtime_module.serialized_kubernetes_worker_validation_snapshot(
        runtime_paths,
        runtime_config=runtime_config,
    )
    second_snapshot = workers_runtime_module.serialized_kubernetes_worker_validation_snapshot(
        runtime_paths,
        runtime_config=runtime_config,
    )

    assert len(calls) == 1
    assert first_snapshot == second_snapshot


def test_serialized_kubernetes_worker_validation_snapshot_clear_recomputes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Manual invalidation should force a fresh resolver call."""
    runtime_paths = _runtime_paths(tmp_path)
    runtime_config = Config()
    calls = 0

    def fake_resolver(*_args: object, **_kwargs: object) -> dict[str, ToolValidationInfo]:
        nonlocal calls
        calls += 1
        return {"fake": ToolValidationInfo(name="fake")}

    monkeypatch.setattr(
        "mindroom.tool_system.catalog.resolved_tool_validation_snapshot_for_runtime",
        fake_resolver,
    )

    workers_runtime_module.serialized_kubernetes_worker_validation_snapshot(
        runtime_paths,
        runtime_config=runtime_config,
    )
    workers_runtime_module.clear_worker_validation_snapshot_cache()
    workers_runtime_module.serialized_kubernetes_worker_validation_snapshot(
        runtime_paths,
        runtime_config=runtime_config,
    )

    assert calls == 2


def test_serialized_kubernetes_worker_validation_snapshot_returns_independent_copies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Callers should not be able to mutate the cached snapshot payload."""
    runtime_paths = _runtime_paths(tmp_path)
    runtime_config = Config()
    calls = 0

    def fake_resolver(*_args: object, **_kwargs: object) -> dict[str, ToolValidationInfo]:
        nonlocal calls
        calls += 1
        return {"fake": ToolValidationInfo(name="fake")}

    monkeypatch.setattr(
        "mindroom.tool_system.catalog.resolved_tool_validation_snapshot_for_runtime",
        fake_resolver,
    )

    first_snapshot = workers_runtime_module.serialized_kubernetes_worker_validation_snapshot(
        runtime_paths,
        runtime_config=runtime_config,
    )
    first_snapshot["fake"]["config_fields"].append({"name": "mutated"})
    second_snapshot = workers_runtime_module.serialized_kubernetes_worker_validation_snapshot(
        runtime_paths,
        runtime_config=runtime_config,
    )

    assert calls == 1
    assert second_snapshot["fake"]["config_fields"] == []


def test_serialized_kubernetes_worker_validation_snapshot_cache_key_includes_mcp_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """MCP config changes should produce a distinct validation snapshot cache key."""
    runtime_paths = _runtime_paths(tmp_path)
    first_config = Config(
        mcp_servers={
            "alpha": {
                "transport": "stdio",
                "command": "alpha-server",
            },
        },
    )
    second_config = Config(
        mcp_servers={
            "beta": {
                "transport": "stdio",
                "command": "beta-server",
            },
        },
    )
    calls = 0

    def fake_resolver(*_args: object, **_kwargs: object) -> dict[str, ToolValidationInfo]:
        nonlocal calls
        calls += 1
        return {"fake": ToolValidationInfo(name="fake")}

    monkeypatch.setattr(
        "mindroom.tool_system.catalog.resolved_tool_validation_snapshot_for_runtime",
        fake_resolver,
    )

    workers_runtime_module.serialized_kubernetes_worker_validation_snapshot(
        runtime_paths,
        runtime_config=first_config,
    )
    workers_runtime_module.serialized_kubernetes_worker_validation_snapshot(
        runtime_paths,
        runtime_config=first_config,
    )
    workers_runtime_module.serialized_kubernetes_worker_validation_snapshot(
        runtime_paths,
        runtime_config=second_config,
    )

    assert calls == 2


def test_serialized_kubernetes_worker_validation_snapshot_cache_key_includes_plugin_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Plugin config changes should produce a distinct validation snapshot cache key."""
    runtime_paths = _runtime_paths(tmp_path)
    first_config = Config(plugins=[{"path": "plugins/one"}])
    second_config = Config(plugins=[{"path": "plugins/two"}])
    calls = 0

    def fake_resolver(*_args: object, **_kwargs: object) -> dict[str, ToolValidationInfo]:
        nonlocal calls
        calls += 1
        return {"fake": ToolValidationInfo(name="fake")}

    monkeypatch.setattr(
        "mindroom.tool_system.catalog.resolved_tool_validation_snapshot_for_runtime",
        fake_resolver,
    )

    workers_runtime_module.serialized_kubernetes_worker_validation_snapshot(
        runtime_paths,
        runtime_config=first_config,
    )
    workers_runtime_module.serialized_kubernetes_worker_validation_snapshot(
        runtime_paths,
        runtime_config=first_config,
    )
    workers_runtime_module.serialized_kubernetes_worker_validation_snapshot(
        runtime_paths,
        runtime_config=second_config,
    )

    assert calls == 2
