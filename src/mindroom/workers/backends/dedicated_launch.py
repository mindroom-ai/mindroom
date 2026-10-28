"""What a dedicated worker's runner receives at startup, shared by the Docker and Kubernetes backends.

Each backend supplies its container layout and the environment it passes on, and adds what only it sets:
its HOME rule, how the runner token arrives, its operator-env rule, and every mount, image, and security setting.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from mindroom.constants import RuntimePaths, sandbox_startup_manifest_path
from mindroom.runtime_env_policy import (
    SANDBOX_RUNTIME_ENV_BY_KEY,
    SANDBOX_STARTUP_MANIFEST_PATH_ENV,
    SHARED_CREDENTIALS_PATH_ENV,
    public_worker_startup_env,
)
from mindroom.tool_system.worker_routing import WORKER_SHARED_CREDENTIALS_DIRNAME

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class DedicatedWorkerLayout:
    """Where one dedicated worker's runner finds its state and config, spelled as its container sees them."""

    dedicated_root: str
    config_path: str
    worker_port: int


def dedicated_worker_runner_env(layout: DedicatedWorkerLayout, *, worker_key: str) -> dict[str, str]:
    """Return the runner variables of one dedicated worker's container, in the order Kubernetes renders them."""
    dedicated_root = layout.dedicated_root
    return {
        SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"]: "true",
        SANDBOX_RUNTIME_ENV_BY_KEY["runner_execution_mode"]: "forkserver",
        SANDBOX_RUNTIME_ENV_BY_KEY["runner_port"]: str(layout.worker_port),
        SANDBOX_STARTUP_MANIFEST_PATH_ENV: str(sandbox_startup_manifest_path(Path(dedicated_root))),
        "MINDROOM_CONFIG_PATH": layout.config_path,
        "MINDROOM_STORAGE_PATH": dedicated_root,
        SHARED_CREDENTIALS_PATH_ENV: f"{dedicated_root}/{WORKER_SHARED_CREDENTIALS_DIRNAME}",
        SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_key"]: worker_key,
        SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_root"]: dedicated_root,
    }


def dedicated_worker_startup_runtime_paths(
    layout: DedicatedWorkerLayout,
    *,
    worker_key: str,
    process_env: Mapping[str, str],
    env_file_values: Mapping[str, str],
) -> RuntimePaths:
    """Return the runtime one dedicated worker's runner boots from, as its startup manifest records it.

    ``process_env`` and ``env_file_values`` are what the backend passes on from the primary and the operator.
    The runner variables override them, and only names the public startup filter allows reach the worker,
    so no secret or primary control value does.
    """
    config_path = Path(layout.config_path)
    dedicated_root = Path(layout.dedicated_root)
    # The manifest spells both paths normalized, whatever spelling the container environment keeps.
    runner_env = dedicated_worker_runner_env(
        DedicatedWorkerLayout(str(dedicated_root), str(config_path), layout.worker_port),
        worker_key=worker_key,
    )
    return RuntimePaths(
        config_path=config_path,
        config_dir=config_path.parent,
        env_path=config_path.parent / ".env",
        storage_root=dedicated_root.resolve(),
        process_env=MappingProxyType(public_worker_startup_env({**process_env, **runner_env})),
        env_file_values=MappingProxyType(public_worker_startup_env(env_file_values)),
    )
