"""Tests for primary-runtime worker validation snapshot caching."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

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


def test_duplicate_worker_manager_build_is_immediately_disposable() -> None:
    """A losing same-signature build does not wait for application shutdown."""
    signature = ("test", "same")
    active_manager = MagicMock()
    duplicate_manager = MagicMock()
    active = workers_runtime_module._WorkerManagerEntry(active_manager, signature)
    previous_active = workers_runtime_module._PRIMARY_WORKER_MANAGER_ENTRY
    previous_retired = workers_runtime_module._RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES
    previous_building = workers_runtime_module._PRIMARY_WORKER_MANAGER_BUILDING_SIGNATURES
    try:
        workers_runtime_module._PRIMARY_WORKER_MANAGER_ENTRY = active
        workers_runtime_module._RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES = []
        workers_runtime_module._PRIMARY_WORKER_MANAGER_BUILDING_SIGNATURES = {signature}

        published = workers_runtime_module._publish_primary_worker_manager_build(
            duplicate_manager,
            signature,
            acquisition_epoch=workers_runtime_module._PRIMARY_WORKER_MANAGER_EPOCH,
            acquire_lease=False,
        )

        assert published == (active, [duplicate_manager])
        assert workers_runtime_module._RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES == []
    finally:
        workers_runtime_module._PRIMARY_WORKER_MANAGER_ENTRY = previous_active
        workers_runtime_module._RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES = previous_retired
        workers_runtime_module._PRIMARY_WORKER_MANAGER_BUILDING_SIGNATURES = previous_building


def test_serialized_dedicated_worker_validation_snapshot_reuses_cached_resolver_result(
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

    first_snapshot = workers_runtime_module.serialized_dedicated_worker_validation_snapshot(
        runtime_paths,
        runtime_config=runtime_config,
    )
    second_snapshot = workers_runtime_module.serialized_dedicated_worker_validation_snapshot(
        runtime_paths,
        runtime_config=runtime_config,
    )

    assert len(calls) == 1
    assert first_snapshot == second_snapshot


def test_serialized_dedicated_worker_validation_snapshot_tolerates_plugin_load_errors(
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

    workers_runtime_module.serialized_dedicated_worker_validation_snapshot(
        runtime_paths,
        runtime_config=runtime_config,
    )

    assert tolerate_values == [True]


def test_serialized_dedicated_worker_validation_snapshot_loads_config_tolerantly(
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

    snapshot = workers_runtime_module.serialized_dedicated_worker_validation_snapshot(runtime_paths)

    assert set(snapshot) == {"fake", "scheduler"}


def test_serialized_dedicated_worker_validation_snapshot_clear_recomputes(
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

    workers_runtime_module.serialized_dedicated_worker_validation_snapshot(
        runtime_paths,
        runtime_config=runtime_config,
    )
    workers_runtime_module.clear_worker_validation_snapshot_cache()
    workers_runtime_module.serialized_dedicated_worker_validation_snapshot(
        runtime_paths,
        runtime_config=runtime_config,
    )

    assert calls == 2


def test_serialized_dedicated_worker_validation_snapshot_returns_independent_copies(
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

    first_snapshot = workers_runtime_module.serialized_dedicated_worker_validation_snapshot(
        runtime_paths,
        runtime_config=runtime_config,
    )
    first_snapshot["fake"]["config_fields"].append({"name": "mutated"})
    second_snapshot = workers_runtime_module.serialized_dedicated_worker_validation_snapshot(
        runtime_paths,
        runtime_config=runtime_config,
    )

    assert calls == 1
    assert second_snapshot["fake"]["config_fields"] == []


def test_serialized_dedicated_worker_validation_snapshot_cache_key_includes_mcp_config(
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

    workers_runtime_module.serialized_dedicated_worker_validation_snapshot(
        runtime_paths,
        runtime_config=first_config,
    )
    workers_runtime_module.serialized_dedicated_worker_validation_snapshot(
        runtime_paths,
        runtime_config=first_config,
    )
    workers_runtime_module.serialized_dedicated_worker_validation_snapshot(
        runtime_paths,
        runtime_config=second_config,
    )

    assert calls == 2


def test_serialized_dedicated_worker_validation_snapshot_cache_key_includes_plugin_entries(
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

    workers_runtime_module.serialized_dedicated_worker_validation_snapshot(
        runtime_paths,
        runtime_config=first_config,
    )
    workers_runtime_module.serialized_dedicated_worker_validation_snapshot(
        runtime_paths,
        runtime_config=first_config,
    )
    workers_runtime_module.serialized_dedicated_worker_validation_snapshot(
        runtime_paths,
        runtime_config=second_config,
    )

    assert calls == 2


def test_configured_primary_worker_manager_lease_uses_one_committed_config_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Lifecycle and cleanup should resolve the same config-aware primary manager."""
    runtime_paths = _runtime_paths(tmp_path)
    runtime_config = Config()
    expected = MagicMock()
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.sandbox_proxy_config",
        lambda _paths: MagicMock(proxy_url="http://worker.test", proxy_token="worker-token"),  # noqa: S106
    )
    monkeypatch.setattr(workers_runtime_module, "primary_worker_backend_available", lambda *_args, **_kwargs: True)
    expected_lease = MagicMock(manager=expected)
    lease_manager = MagicMock(return_value=expected_lease)
    monkeypatch.setattr(workers_runtime_module, "lease_primary_worker_manager", lease_manager)

    resolved = workers_runtime_module.lease_configured_primary_worker_manager(
        runtime_paths,
        runtime_config=runtime_config,
    )

    assert resolved is expected_lease
    assert lease_manager.call_args.kwargs["storage_root"] == runtime_paths.storage_root
    assert lease_manager.call_args.kwargs["worker_grantable_credentials"] == (
        runtime_config.get_worker_grantable_credentials()
    )


def test_configured_docker_worker_manager_receives_validation_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The Docker manager should receive the committed primary tool-validation snapshot."""
    runtime_paths = _runtime_paths(tmp_path)
    runtime_config = Config()
    validation_snapshot = {"shell": {"name": "shell", "config_fields": []}}
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.sandbox_proxy_config",
        lambda _paths: MagicMock(proxy_url="http://worker.test", proxy_token="worker-token"),  # noqa: S106
    )
    monkeypatch.setattr(workers_runtime_module, "primary_worker_backend_available", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(workers_runtime_module, "primary_worker_backend_name", lambda _paths: "docker")
    monkeypatch.setattr(
        workers_runtime_module,
        "serialized_dedicated_worker_validation_snapshot",
        MagicMock(return_value=validation_snapshot),
    )
    lease_manager = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(workers_runtime_module, "lease_primary_worker_manager", lease_manager)

    workers_runtime_module.lease_configured_primary_worker_manager(
        runtime_paths,
        runtime_config=runtime_config,
    )

    assert lease_manager.call_args.kwargs["dedicated_worker_validation_snapshot"] == validation_snapshot


def test_docker_worker_manager_build_passes_validation_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Docker backend construction should retain the committed validation snapshot."""
    runtime_paths = _runtime_paths(tmp_path)
    validation_snapshot = {"shell": {"name": "shell", "config_fields": []}}
    expected = MagicMock()
    from_runtime = MagicMock(return_value=expected)
    monkeypatch.setattr(workers_runtime_module, "primary_worker_backend_name", lambda _paths: "docker")
    monkeypatch.setattr(workers_runtime_module.DockerWorkerBackend, "from_runtime", from_runtime)

    resolved = workers_runtime_module._build_primary_worker_manager(
        runtime_paths,
        proxy_url=None,
        proxy_token="worker-token",  # noqa: S106
        storage_root=runtime_paths.storage_root,
        dedicated_worker_validation_snapshot=validation_snapshot,
    )

    assert resolved is expected
    assert from_runtime.call_args.kwargs["tool_validation_snapshot"] == validation_snapshot


def test_docker_worker_manager_identity_tracks_validation_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Adding a committed snapshot should replace a snapshot-less Docker manager."""
    runtime_paths = _runtime_paths(tmp_path)
    validation_snapshot = {"shell": {"name": "shell", "config_fields": []}}
    monkeypatch.setattr(workers_runtime_module, "primary_worker_backend_name", lambda _paths: "docker")
    monkeypatch.setattr(
        workers_runtime_module,
        "docker_backend_config_signature",
        lambda *_args, **_kwargs: ("docker", "base"),
    )

    without_snapshot = workers_runtime_module._primary_worker_backend_config_signature(
        runtime_paths,
        proxy_url=None,
        proxy_token="worker-token",  # noqa: S106
        storage_root=runtime_paths.storage_root,
    )
    with_snapshot = workers_runtime_module._primary_worker_backend_config_signature(
        runtime_paths,
        proxy_url=None,
        proxy_token="worker-token",  # noqa: S106
        storage_root=runtime_paths.storage_root,
        dedicated_worker_validation_snapshot=validation_snapshot,
    )

    assert with_snapshot != without_snapshot


def test_configured_primary_worker_manager_identity_uses_the_lease_signature_without_constructing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A reload can compare one committed worker identity without publishing a manager."""
    runtime_paths = _runtime_paths(tmp_path)
    runtime_config = Config()
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.sandbox_proxy_config",
        lambda _paths: MagicMock(proxy_url="http://worker.test", proxy_token="worker-token"),  # noqa: S106
    )
    monkeypatch.setattr(workers_runtime_module, "primary_worker_backend_available", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(workers_runtime_module, "primary_worker_backend_name", lambda _paths: "docker")
    signature = MagicMock(return_value=("signature",))
    monkeypatch.setattr(workers_runtime_module, "_primary_worker_backend_config_signature", signature)
    build_manager = MagicMock()
    monkeypatch.setattr(workers_runtime_module, "_build_primary_worker_manager", build_manager)

    identity = workers_runtime_module.configured_primary_worker_manager_identity(runtime_paths, runtime_config)

    assert identity == "645b967075b7e04dcf2484456e24e777ae602b20c0bf8b0414bd06d9aaffaed6"
    assert signature.call_args.kwargs["storage_root"] == runtime_paths.storage_root
    build_manager.assert_not_called()


def test_configured_worker_lease_requires_the_current_durable_backend_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Historical backends fail closed instead of being reconstructed from durable state."""
    runtime_paths = _runtime_paths(tmp_path)
    runtime_config = Config()
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.sandbox_proxy_config",
        lambda _paths: MagicMock(proxy_url="http://worker.test", proxy_token="worker-token"),  # noqa: S106
    )
    monkeypatch.setattr(workers_runtime_module, "primary_worker_backend_available", lambda *_args, **_kwargs: True)
    manager = MagicMock(cleanup_locator="current-identity")
    lease = MagicMock(manager=manager)
    monkeypatch.setattr(workers_runtime_module, "lease_primary_worker_manager", MagicMock(return_value=lease))

    matched = workers_runtime_module.lease_configured_primary_worker_manager(
        runtime_paths,
        runtime_config=runtime_config,
        required_backend_locator="current-identity",
    )
    mismatched = workers_runtime_module.lease_configured_primary_worker_manager(
        runtime_paths,
        runtime_config=runtime_config,
        required_backend_locator="historical-identity",
    )

    assert matched is lease
    assert mismatched is None
    lease.release.assert_called_once_with()


def test_kubernetes_cleanup_identity_ignores_launch_config_but_tracks_resource_owner(tmp_path: Path) -> None:
    """Routine config changes retain cleanup access without crossing resource owners."""
    storage_root = tmp_path / "shared-storage"
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=storage_root,
        process_env={
            "MINDROOM_WORKER_BACKEND": "kubernetes",
            "MINDROOM_KUBERNETES_WORKER_IMAGE": "test-image",
            "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME": "test-pvc",
        },
    )
    other_namespace_paths = replace(
        runtime_paths,
        process_env={
            **runtime_paths.process_env,
            "MINDROOM_KUBERNETES_WORKER_NAMESPACE": "other-namespace",
        },
    )

    base = workers_runtime_module._primary_worker_backend_cleanup_signature(
        runtime_paths,
        storage_root=storage_root,
        config_signature=("launch-config-a",),
    )

    assert base == workers_runtime_module._primary_worker_backend_cleanup_signature(
        runtime_paths,
        storage_root=storage_root,
        config_signature=("launch-config-b",),
    )
    assert base != workers_runtime_module._primary_worker_backend_cleanup_signature(
        other_namespace_paths,
        storage_root=storage_root,
        config_signature=("launch-config-a",),
    )
    assert base != workers_runtime_module._primary_worker_backend_cleanup_signature(
        runtime_paths,
        storage_root=tmp_path / "other-storage",
        config_signature=("launch-config-a",),
    )


def test_configured_primary_worker_manager_lease_skips_kubernetes_without_runtime_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Kubernetes maintenance must wait for a committed config snapshot."""
    runtime_paths = _runtime_paths(tmp_path)
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.sandbox_proxy_config",
        lambda _paths: MagicMock(proxy_url="http://worker.test", proxy_token="worker-token"),  # noqa: S106
    )
    monkeypatch.setattr(workers_runtime_module, "primary_worker_backend_available", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(workers_runtime_module, "primary_worker_backend_name", lambda _paths: "kubernetes")
    lease_manager = MagicMock()
    monkeypatch.setattr(workers_runtime_module, "lease_primary_worker_manager", lease_manager)

    resolved = workers_runtime_module.lease_configured_primary_worker_manager(
        runtime_paths,
        runtime_config=None,
    )

    assert resolved is None
    lease_manager.assert_not_called()
