"""Tests for the primary worker manager, its identity, and the launch config dedicated workers use."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.tool_system.catalog import serialize_tool_validation_snapshot
from mindroom.tool_system.plugins import config_plugins
from mindroom.workers import runtime as workers_runtime_module
from mindroom.workers.backend import WorkerBackendError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _reset_worker_launch_config() -> Iterator[None]:
    """Isolate the process-local launch dedicated workers derived."""
    workers_runtime_module.reset_worker_launch_config()
    yield
    workers_runtime_module.reset_worker_launch_config()


@pytest.fixture
def _fresh_primary_worker_manager() -> Iterator[None]:
    """Drop the primary worker manager a test publishes into the process-wide slot."""
    workers_runtime_module.reset_primary_worker_manager()
    yield
    workers_runtime_module.reset_primary_worker_manager()


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
        )

        assert published == (active, [duplicate_manager])
        assert workers_runtime_module._RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES == []
    finally:
        workers_runtime_module._PRIMARY_WORKER_MANAGER_ENTRY = previous_active
        workers_runtime_module._RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES = previous_retired
        workers_runtime_module._PRIMARY_WORKER_MANAGER_BUILDING_SIGNATURES = previous_building


def test_dedicated_worker_validation_snapshot_is_the_applied_config_s_registry(tmp_path: Path) -> None:
    """Workers start from the validation view of the tools the applied config can build, its MCP servers included."""
    runtime_paths = _runtime_paths(tmp_path)
    runtime_config = Config(mcp_servers={"alpha": {"transport": "stdio", "command": "alpha-server"}})

    snapshot = workers_runtime_module.serialized_dedicated_worker_validation_snapshot(
        runtime_paths,
        runtime_config=runtime_config,
    )

    assert snapshot == serialize_tool_validation_snapshot(
        config_plugins(runtime_config, runtime_paths).tools.validation_snapshot,
    )
    assert "mcp_alpha" in snapshot
    assert "shell" in snapshot


def test_dedicated_worker_validation_snapshot_tolerates_a_plugin_that_does_not_load(tmp_path: Path) -> None:
    """A configured plugin the primary could not load leaves the snapshot buildable, without its tools."""
    runtime_paths = _runtime_paths(tmp_path)
    runtime_config = Config(plugins=[{"path": "plugins/broken"}])

    snapshot = workers_runtime_module.serialized_dedicated_worker_validation_snapshot(
        runtime_paths,
        runtime_config=runtime_config,
    )

    assert "shell" in snapshot


def test_configured_primary_worker_manager_lease_uses_backend_settings_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Lifecycle and cleanup lease the primary manager from backend settings, never from a config."""
    runtime_paths = _runtime_paths(tmp_path)
    expected = MagicMock()
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.sandbox_proxy_config",
        lambda _paths: MagicMock(proxy_url="http://worker.test", proxy_token="worker-token"),  # noqa: S106
    )
    monkeypatch.setattr(workers_runtime_module, "primary_worker_backend_available", lambda *_args, **_kwargs: True)
    expected_lease = MagicMock(manager=expected)
    lease_manager = MagicMock(return_value=expected_lease)
    monkeypatch.setattr(workers_runtime_module, "lease_primary_worker_manager", lease_manager)

    resolved = workers_runtime_module.lease_configured_primary_worker_manager(runtime_paths)

    assert resolved is expected_lease
    assert lease_manager.call_args.kwargs == {
        "proxy_url": "http://worker.test",
        "proxy_token": "worker-token",
        "storage_root": runtime_paths.storage_root,
    }


def test_configured_primary_worker_manager_identity_uses_the_lease_signature_without_constructing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A reload can compare one committed worker identity without publishing a manager."""
    runtime_paths = _runtime_paths(tmp_path)
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

    identity = workers_runtime_module.configured_primary_worker_manager_identity(runtime_paths)

    assert identity == "645b967075b7e04dcf2484456e24e777ae602b20c0bf8b0414bd06d9aaffaed6"
    assert signature.call_args.kwargs["storage_root"] == runtime_paths.storage_root
    build_manager.assert_not_called()


def test_configured_worker_lease_requires_the_current_durable_backend_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Historical backends fail closed instead of being reconstructed from durable state."""
    runtime_paths = _runtime_paths(tmp_path)
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
        required_backend_locator="current-identity",
    )
    mismatched = workers_runtime_module.lease_configured_primary_worker_manager(
        runtime_paths,
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


def _kubernetes_runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Return a runtime path set whose primary worker backend is Kubernetes."""
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={
            "MINDROOM_WORKER_BACKEND": "kubernetes",
            "MINDROOM_KUBERNETES_WORKER_IMAGE": "test-image",
            "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME": "test-pvc",
        },
    )


def _count_validation_snapshots(monkeypatch: pytest.MonkeyPatch) -> list[Config]:
    """Replace the tool validation snapshot with a stub that records each config it is built for."""
    built_for: list[Config] = []

    def fake_snapshot(_runtime_paths: RuntimePaths, *, runtime_config: Config) -> dict[str, dict[str, object]]:
        built_for.append(runtime_config)
        return {"shell": {"name": "shell", "call": len(built_for)}}

    monkeypatch.setattr(workers_runtime_module, "serialized_dedicated_worker_validation_snapshot", fake_snapshot)
    return built_for


def test_bind_worker_launch_config_source_refuses_a_second_source() -> None:
    """Only one owner of the applied config may feed worker launches in a process."""
    with (
        workers_runtime_module.bind_worker_launch_config_source(Config),
        pytest.raises(RuntimeError, match="already bound"),
        workers_runtime_module.bind_worker_launch_config_source(Config),
    ):
        pass
    with workers_runtime_module.bind_worker_launch_config_source(Config):
        pass


def test_current_worker_launch_config_is_derived_once_per_applied_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Every operation on one applied config shares its launch, and a new applied config gets its own."""
    runtime_paths = _runtime_paths(tmp_path)
    built_for = _count_validation_snapshots(monkeypatch)
    first = Config(defaults={"worker_grantable_credentials": ["openai"]})
    second = Config(defaults={"worker_grantable_credentials": ["github_private"]})
    applied = [first]

    with workers_runtime_module.bind_worker_launch_config_source(lambda: applied[-1]):
        launch = workers_runtime_module.current_worker_launch_config(runtime_paths)
        assert workers_runtime_module.current_worker_launch_config(runtime_paths) is launch
        applied.append(second)
        next_launch = workers_runtime_module.current_worker_launch_config(runtime_paths)

    assert built_for == [first, second]
    assert launch.config is first
    assert launch.grantable_credentials == frozenset({"openai"})
    assert launch.tool_validation_snapshot == {"shell": {"name": "shell", "call": 1}}
    assert next_launch.config is second
    assert next_launch.grantable_credentials == frozenset({"github_private"})
    assert next_launch != launch


def test_current_worker_launch_config_rebuilds_after_a_plugin_reload_of_the_same_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A plugin reload keeps the same config object, so clearing the snapshot cache must drop its launch too."""
    runtime_paths = _runtime_paths(tmp_path)
    built_for = _count_validation_snapshots(monkeypatch)
    config = Config()

    with workers_runtime_module.bind_worker_launch_config_source(lambda: config):
        launch = workers_runtime_module.current_worker_launch_config(runtime_paths)
        workers_runtime_module.reset_worker_launch_config()
        reloaded = workers_runtime_module.current_worker_launch_config(runtime_paths)

    assert built_for == [config, config]
    assert reloaded is not launch
    assert reloaded.config is config
    assert reloaded.tool_validation_snapshot == {"shell": {"name": "shell", "call": 2}}
    assert reloaded.digest != launch.digest


def test_current_worker_launch_config_refuses_without_an_applied_config(tmp_path: Path) -> None:
    """No bound source, or a source with nothing applied yet, means no dedicated worker launch."""
    runtime_paths = _runtime_paths(tmp_path)

    with pytest.raises(WorkerBackendError, match="Dedicated workers need the applied config"):
        workers_runtime_module.current_worker_launch_config(runtime_paths)
    with (
        workers_runtime_module.bind_worker_launch_config_source(lambda: None),
        pytest.raises(WorkerBackendError, match="Dedicated workers need the applied config"),
    ):
        workers_runtime_module.current_worker_launch_config(runtime_paths)


@pytest.mark.usefixtures("_fresh_primary_worker_manager")
def test_configured_kubernetes_worker_manager_lease_needs_no_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Kubernetes maintenance and status lease the manager before any config is applied."""
    runtime_paths = _kubernetes_runtime_paths(tmp_path)
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.sandbox_proxy_config",
        lambda _paths: MagicMock(proxy_url=None, proxy_token="worker-token"),  # noqa: S106
    )
    build_manager = MagicMock(return_value=MagicMock(backend_name="kubernetes"))
    monkeypatch.setattr(workers_runtime_module, "_build_primary_worker_manager", build_manager)

    lease = workers_runtime_module.lease_configured_primary_worker_manager(runtime_paths)

    assert lease is not None
    with lease as manager:
        assert manager is build_manager.return_value
    build_manager.assert_called_once()


@pytest.mark.usefixtures("_fresh_primary_worker_manager")
def test_primary_worker_manager_identity_ignores_the_applied_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A config reload neither replaces the worker manager nor changes its identity."""
    runtime_paths = _kubernetes_runtime_paths(tmp_path)
    _count_validation_snapshots(monkeypatch)
    monkeypatch.setattr(
        "mindroom.tool_system.sandbox_proxy.sandbox_proxy_config",
        lambda _paths: MagicMock(proxy_url=None, proxy_token="worker-token"),  # noqa: S106
    )
    build_manager = MagicMock(side_effect=lambda *_args, **_kwargs: MagicMock(backend_name="kubernetes"))
    monkeypatch.setattr(workers_runtime_module, "_build_primary_worker_manager", build_manager)
    applied = [Config(defaults={"worker_grantable_credentials": ["openai"]})]
    managers: list[object] = []
    identities: list[str | None] = []
    launches: list[object] = []

    with workers_runtime_module.bind_worker_launch_config_source(lambda: applied[-1]):
        for next_config in (Config(agents={"helper": {"display_name": "Helper"}}), None):
            lease = workers_runtime_module.lease_configured_primary_worker_manager(runtime_paths)
            assert lease is not None
            with lease as manager:
                managers.append(manager)
            identities.append(workers_runtime_module.configured_primary_worker_manager_identity(runtime_paths))
            launches.append(workers_runtime_module.current_worker_launch_config(runtime_paths))
            if next_config is not None:
                applied.append(next_config)

    assert build_manager.call_count == 1
    assert managers[0] is managers[1]
    assert identities[0] == identities[1] is not None
    assert launches[0] != launches[1]
