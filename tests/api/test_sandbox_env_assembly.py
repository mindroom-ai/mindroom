"""Unit tests for the canonical request execution-environment assembly.

These exercise :func:`build_request_execution_env` directly with plain inputs
(no FastAPI app, no subprocess, no agent routing), which is the whole point of
splitting the assembly ordering out of the sandbox runner: the
security-sensitive precedence is now testable in isolation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import mindroom.api.sandbox_env_assembly as assembly
from mindroom import constants
from mindroom.api.sandbox_exec import WorkspaceEnvHookError


def _write_hook(workspace: Path, body: str) -> None:
    hook_dir = workspace / ".mindroom"
    hook_dir.mkdir(parents=True, exist_ok=True)
    (hook_dir / "worker-env.sh").write_text(body, encoding="utf-8")


def test_request_workspace_none_is_noop() -> None:
    """With no resolved workspace, the assembler leaves the env untouched."""
    execution_env = {"PATH": "/usr/bin:/bin"}
    result = assembly.build_request_execution_env(
        request_workspace=None,
        prepared=None,
        execution_env=execution_env,
    )
    assert result.workspace_home is None
    assert result.trusted_overlay == {}
    assert execution_env == {"PATH": "/usr/bin:/bin"}


def test_home_contract_applied_without_hook(tmp_path: Path) -> None:
    """A resolved workspace seeds the HOME contract even when no hook exists."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    execution_env: dict[str, str] = {}
    result = assembly.build_request_execution_env(
        request_workspace=workspace,
        prepared=None,
        execution_env=execution_env,
    )
    assert result.workspace_home == workspace.resolve()
    assert execution_env["HOME"] == str(workspace.resolve())
    assert execution_env["MINDROOM_AGENT_WORKSPACE"] == str(workspace.resolve())


def test_apply_home_contract_false_keeps_workspace_home_none(tmp_path: Path) -> None:
    """Skipping the contract must report ``workspace_home`` as None.

    This is the trusted-child subprocess re-execution path: the caller keys
    protected-name filtering off the result, and a non-None ``workspace_home``
    here would start protecting the HOME-contract names and change behavior.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    execution_env = {"HOME": "/request-home"}
    result = assembly.build_request_execution_env(
        request_workspace=workspace,
        prepared=None,
        execution_env=execution_env,
        apply_workspace_home_contract=False,
        apply_workspace_env_hook=False,
    )
    assert result.workspace_home is None
    # No prepared worker and no contract -> nothing is protected, request HOME survives.
    assert execution_env["HOME"] == "/request-home"
    assert assembly.protected_execution_env_names(workspace_home=None, prepared=None) == frozenset()


def test_hook_cannot_override_home_contract(tmp_path: Path) -> None:
    """The contract wins over the hook, but the hook still observed the contract."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_hook(
        workspace,
        'export HOOK_SAW_HOME="$HOME"\nexport HOME=/hook-home\nexport WORKSPACE_TOOLCHAIN_PATH=/hook/bin\n',
    )
    execution_env: dict[str, str] = {}
    result = assembly.build_request_execution_env(
        request_workspace=workspace,
        prepared=None,
        execution_env=execution_env,
    )
    # Canonical ordering: HOME contract runs before the hook (the hook saw it)...
    assert result.trusted_overlay["HOOK_SAW_HOME"] == str(workspace.resolve())
    # ...and the hook cannot redirect HOME in the final env.
    assert execution_env["HOME"] == str(workspace.resolve())
    assert "HOME" not in result.trusted_overlay
    # Non-protected hook exports survive in the trusted overlay.
    assert result.trusted_overlay["WORKSPACE_TOOLCHAIN_PATH"] == "/hook/bin"
    assert execution_env["WORKSPACE_TOOLCHAIN_PATH"] == "/hook/bin"


def test_hook_failure_raises_workspace_env_hook_error(tmp_path: Path) -> None:
    """A failing hook surfaces as WorkspaceEnvHookError for the caller to map once."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_hook(workspace, 'echo "bad hook" >&2\nexit 7\n')
    with pytest.raises(WorkspaceEnvHookError):
        assembly.build_request_execution_env(
            request_workspace=workspace,
            prepared=None,
            execution_env={},
        )


def test_apply_hook_false_skips_sourcing(tmp_path: Path) -> None:
    """The re-execution path can skip a hook that would otherwise fail."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_hook(workspace, "exit 9\n")  # would raise if sourced
    result = assembly.build_request_execution_env(
        request_workspace=workspace,
        prepared=None,
        execution_env={},
        apply_workspace_env_hook=False,
    )
    assert result.trusted_overlay == {}


def test_protected_execution_env_names_variants() -> None:
    """Protected-name set depends on whether a workspace home or worker is present."""
    assert assembly.protected_execution_env_names(workspace_home=None, prepared=None) == frozenset()
    assert (
        assembly.protected_execution_env_names(workspace_home=Path("/ws"), prepared=None)
        == constants.WORKSPACE_HOME_CONTRACT_ENV_NAMES
    )


def test_trusted_overlay_filters_protected_names() -> None:
    """Protected names are stripped from the overlay; an empty set is a pass-through."""
    overlay = {"A": "1", "HOME": "/x", "B": "2"}
    assert assembly.trusted_workspace_overlay_for_runtime_paths(overlay, {"HOME"}) == {"A": "1", "B": "2"}
    # An empty protected set returns the overlay unchanged.
    assert assembly.trusted_workspace_overlay_for_runtime_paths(overlay, frozenset()) == overlay
