"""Tests for primary-runtime worker validation snapshots."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from mindroom.config.main import Config, RuntimeConfig
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.tool_system.catalog import (
    TOOL_METADATA,
    bind_resolved_tool_state_cache,
    resolved_tool_runtime_state_from_registry,
)
from mindroom.tool_system.metadata import ToolValidationInfo
from mindroom.workers import runtime as workers_runtime_module

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Return a runtime path set rooted under one pytest temp directory."""
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )


def test_serialized_kubernetes_worker_validation_snapshot_uses_each_runtime_config_carried_state(
    tmp_path: Path,
) -> None:
    """Same-authored runtime configs must retain their distinct resolved catalogs."""
    runtime_paths = _runtime_paths(tmp_path)
    authored = Config(defaults={"tools": []})
    first_state = resolved_tool_runtime_state_from_registry(
        runtime_paths,
        authored,
        {},
        {"first": replace(TOOL_METADATA["shell"], name="first")},
    )
    second_state = resolved_tool_runtime_state_from_registry(
        runtime_paths,
        authored,
        {},
        {"second": replace(TOOL_METADATA["shell"], name="second")},
    )
    first_config = RuntimeConfig.from_authored(
        authored,
        runtime_paths,
        tolerate_plugin_load_errors=True,
        tool_validation_snapshot=first_state.validation_snapshot,
    )
    second_config = RuntimeConfig.from_authored(
        authored,
        runtime_paths,
        tolerate_plugin_load_errors=True,
        tool_validation_snapshot=second_state.validation_snapshot,
    )
    bind_resolved_tool_state_cache(first_state, first_config)
    bind_resolved_tool_state_cache(second_state, second_config)

    first_snapshot = workers_runtime_module.serialized_kubernetes_worker_validation_snapshot(
        runtime_paths,
        runtime_config=first_config,
    )
    second_snapshot = workers_runtime_module.serialized_kubernetes_worker_validation_snapshot(
        runtime_paths,
        runtime_config=second_config,
    )

    assert set(first_snapshot) == {"first"}
    assert set(second_snapshot) == {"second"}


def test_serialized_kubernetes_worker_validation_snapshot_tolerates_plugin_load_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker validation snapshots should match the tolerant primary startup path."""
    runtime_paths = _runtime_paths(tmp_path)
    runtime_config = Config(plugins=[{"path": "plugins/broken"}])
    tolerate_values: list[object] = []

    def fake_resolver(*_args: object, **kwargs: object) -> dict[str, ToolValidationInfo]:
        tolerate_values.append(kwargs.get("tolerate_plugin_load_errors"))
        return {"fake": ToolValidationInfo(name="fake")}

    monkeypatch.setattr(
        "mindroom.tool_system.catalog.resolved_tool_validation_snapshot_for_runtime",
        fake_resolver,
    )

    workers_runtime_module.serialized_kubernetes_worker_validation_snapshot(
        runtime_paths,
        runtime_config=runtime_config,
    )

    assert tolerate_values == [True]


def test_serialized_kubernetes_worker_validation_snapshot_loads_config_tolerantly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The default config-loading branch should match tolerant startup behavior."""
    runtime_paths = _runtime_paths(tmp_path)
    runtime_paths.config_path.write_text(
        (
            "models:\n"
            "  default:\n"
            "    provider: openai\n"
            "    id: gpt-5.4\n"
            "router:\n"
            "  model: default\n"
            "agents: {}\n"
            "plugins:\n"
            "  - ./plugins/missing\n"
        ),
        encoding="utf-8",
    )

    def fake_resolver(*_args: object, **_kwargs: object) -> dict[str, ToolValidationInfo]:
        return {
            "fake": ToolValidationInfo(name="fake"),
            "scheduler": ToolValidationInfo(name="scheduler"),
        }

    monkeypatch.setattr(
        "mindroom.tool_system.catalog.resolved_tool_validation_snapshot_for_runtime",
        fake_resolver,
    )

    snapshot = workers_runtime_module.serialized_kubernetes_worker_validation_snapshot(runtime_paths)

    assert set(snapshot) == {"fake", "scheduler"}


def test_serialized_kubernetes_worker_validation_snapshot_returns_independent_copies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Each serialization should return payloads callers can mutate independently."""
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

    assert calls == 2
    assert second_snapshot["fake"]["config_fields"] == []
