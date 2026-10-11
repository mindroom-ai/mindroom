"""Detached API tool calls retain their own worker config and credential policy."""

from dataclasses import replace
from pathlib import Path

import pytest

from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.runtime_context import (
    WorkerRuntimeContext,
    get_worker_runtime_context,
    worker_runtime_context,
)
from mindroom.tool_system.sandbox_proxy import _primary_worker_manager_context


@pytest.mark.parametrize("backend", ["docker", "kubernetes"])
def test_detached_worker_context_carries_snapshot_and_credential_policy(tmp_path: Path, backend: str) -> None:
    """Losing the API snapshot must not silently widen worker credential grants."""
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_WORKER_BACKEND": backend},
    )
    config = Config.model_validate({"agents": {}, "defaults": {"worker_grantable_credentials": ["gmail"]}})
    context = WorkerRuntimeContext(paths, config, tmp_path / "request-storage")
    with worker_runtime_context(context):
        result = _primary_worker_manager_context(paths)
        assert result.storage_root == tmp_path / "request-storage"
        assert result.worker_grantable_credentials == frozenset({"gmail"})
        assert result.dedicated_worker_validation_snapshot is not None
        assert (result.kubernetes_config_snapshot is not None) == (backend == "kubernetes")
        with (
            worker_runtime_context(replace(context, runtime_paths=replace(paths, storage_root=tmp_path / "other"))),
            pytest.raises(ValueError, match="runtime"),
        ):
            _primary_worker_manager_context(paths)
        assert get_worker_runtime_context() is context
    assert get_worker_runtime_context() is None
