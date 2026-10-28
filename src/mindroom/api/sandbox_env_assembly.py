"""Canonical request execution-environment assembly for the sandbox runner.

This module owns the security-sensitive ordering that turns a request's raw
execution env into the final env handed to a tool: apply MindRoom's workspace
HOME contract, compute the env names MindRoom owns, source the workspace
``.mindroom/worker-env.sh`` hook, then layer the trusted hook overlay *underneath*
the protected names so a hook can never redirect a protected path.

Workspace *resolution* (which workspace applies to a request, including agent
routing) stays in :mod:`mindroom.api.sandbox_runner`; this module receives an
already-resolved workspace and is free of routing/config dependencies, so the
ordering is unit testable in isolation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom import constants
from mindroom.api import sandbox_exec

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from mindroom.api import sandbox_worker_prep


def build_request_execution_env(
    *,
    request_workspace: Path | None,
    prepared: sandbox_worker_prep.PreparedWorkerRequest | None,
    execution_env: dict[str, str],
) -> dict[str, str]:
    """Apply request env overlays in the security-sensitive canonical order and return the trusted hook overlay.

    ``request_workspace`` is the already-resolved workspace for this request, or
    ``None`` when the tool does not support workspace hooks or no workspace
    applies. ``execution_env`` is mutated in place.

    Raises :class:`sandbox_exec.WorkspaceEnvHookError` when the workspace hook
    exists but fails to source.
    """
    if request_workspace is None:
        execution_env.update(_worker_owned_env(prepared))
        return {}
    protected_env = _workspace_home_contract_env(workspace=request_workspace.expanduser().resolve(), prepared=prepared)
    # The hook is sourced with MindRoom's workspace-home defaults already in place.
    execution_env.update(protected_env)
    if prepared is None:
        protected_env |= _existing_worker_runtime_env(execution_env)
    overlay = _workspace_env_overlay(
        request_workspace=request_workspace,
        prepared=prepared,
        execution_env=execution_env,
    )
    trusted_overlay = {
        name: value for name, value in overlay.items() if name not in constants.WORKSPACE_HOME_CONTRACT_ENV_NAMES
    }
    execution_env.update(trusted_overlay)
    execution_env.update(protected_env)
    return trusted_overlay


def _workspace_home_contract_env(
    *,
    workspace: Path,
    prepared: sandbox_worker_prep.PreparedWorkerRequest | None,
) -> dict[str, str]:
    """Build the env contract for an already-resolved worker workspace."""
    return constants.workspace_home_identity_env(workspace) | _worker_owned_env(prepared)


def _worker_owned_env(prepared: sandbox_worker_prep.PreparedWorkerRequest | None) -> dict[str, str]:
    """Return env names that must stay owned by the prepared worker runtime."""
    if prepared is not None:
        worker_env = {
            "XDG_CACHE_HOME": str(prepared.paths.cache_dir),
            "PIP_CACHE_DIR": str(prepared.paths.cache_dir / "pip"),
            "UV_CACHE_DIR": str(prepared.paths.cache_dir / "uv"),
            "PYTHONPYCACHEPREFIX": str(prepared.paths.cache_dir / "pycache"),
            "TMPDIR": str(sandbox_exec.worker_tmp_dir(prepared.paths)),
            "VIRTUAL_ENV": str(prepared.paths.venv_dir),
        }
        for name in constants.WORKER_RUNTIME_PATH_ENV_NAMES:
            worker_env.setdefault(name, "")
        return worker_env
    return {}


def _existing_worker_runtime_env(
    execution_env: Mapping[str, str],
) -> dict[str, str]:
    """Return existing worker-runtime env values to preserve when no worker was prepared."""
    return {name: execution_env[name] for name in constants.WORKER_RUNTIME_PATH_ENV_NAMES if name in execution_env}


def _workspace_env_overlay(
    *,
    request_workspace: Path,
    prepared: sandbox_worker_prep.PreparedWorkerRequest | None,
    execution_env: dict[str, str],
) -> dict[str, str]:
    """Source ``.mindroom/worker-env.sh`` for one request.

    Returns the hook overlay, empty when the hook is absent. Raises
    :class:`sandbox_exec.WorkspaceEnvHookError` when the hook exists but fails to source.
    """
    hook_path = sandbox_exec.resolve_workspace_env_hook_path(request_workspace)
    if hook_path is None:
        return {}
    base_env = _workspace_env_overlay_base_env(prepared, execution_env)
    return sandbox_exec.source_workspace_env_hook(
        hook_path=hook_path,
        base_env=base_env,
        cwd=request_workspace,
    )


def _workspace_env_overlay_base_env(
    prepared: sandbox_worker_prep.PreparedWorkerRequest | None,
    execution_env: dict[str, str],
) -> dict[str, str]:
    base_env = dict(execution_env)
    # Seed PATH/HOME defaults so bash can locate `printenv` when sourcing the
    # hook for inprocess unkeyed proxy calls (the subprocess path already gets
    # these via worker_subprocess_env / generic_subprocess_env).
    if prepared is None:
        for key, value in sandbox_exec.generic_subprocess_env().items():
            base_env.setdefault(key, value)
    return base_env
