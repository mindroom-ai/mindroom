"""Tests for the agent-editable `.mindroom/worker-env.sh` overlay helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mindroom.api import sandbox_exec
from mindroom.api.sandbox_exec import (
    _WORKSPACE_ENV_HOOK_RELATIVE_PATH,
    WorkspaceEnvHookError,
    resolve_workspace_env_hook_path,
    source_workspace_env_hook,
)
from mindroom.constants import resolve_runtime_paths, shell_execution_runtime_env_values

REQUIRES_BASH = pytest.mark.skipif(
    (sys.platform != "linux" and sys.platform != "darwin")
    or (shutil.which("bash") is None and not os.access("/bin/bash", os.X_OK)),
    reason="bash hook execution is validated on POSIX hosts",
)


def _write_hook(workspace: Path, body: str) -> Path:
    hook_dir = workspace / ".mindroom"
    hook_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hook_dir / "worker-env.sh"
    hook_path.write_text(body, encoding="utf-8")
    return hook_path


def test_trusted_workspace_env_overlay_wins_over_execution_env_path(tmp_path: Path) -> None:
    """Hook-exported PATH should not be overwritten by the runner's base PATH."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"PATH": "/usr/bin:/bin"},
    )

    overlaid_paths = sandbox_exec.runtime_paths_with_execution_env(
        runtime_paths,
        {"PATH": "/usr/bin:/bin"},
        trusted_env_overlay={"PATH": "/workspace/.local/bin:/usr/bin:/bin"},
    )
    shell_env = shell_execution_runtime_env_values(overlaid_paths)

    assert shell_env["PATH"].startswith("/workspace/.local/bin:")


def test_resolve_workspace_env_hook_path_returns_none_when_missing(tmp_path: Path) -> None:
    """Resolution returns None when no hook script is present."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert resolve_workspace_env_hook_path(workspace) is None


def test_resolve_workspace_env_hook_path_returns_resolved_file(tmp_path: Path) -> None:
    """Resolution returns the resolved file path when the hook exists."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    written = _write_hook(workspace, "export FOO=bar\n")

    resolved = resolve_workspace_env_hook_path(workspace)

    assert resolved == written.resolve()


def test_resolve_workspace_env_hook_path_returns_none_for_none_base_dir() -> None:
    """Resolution skips silently when no base_dir is provided."""
    assert resolve_workspace_env_hook_path(None) is None


def test_resolve_workspace_env_hook_path_rejects_symlink_escape(tmp_path: Path) -> None:
    """A `.mindroom/worker-env.sh` symlink that escapes the workspace fails closed."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "evil.sh"
    target.write_text("export FOO=leaked\n", encoding="utf-8")
    hook_dir = workspace / ".mindroom"
    hook_dir.mkdir()
    (hook_dir / "worker-env.sh").symlink_to(target)

    with pytest.raises(WorkspaceEnvHookError, match="resolves outside"):
        resolve_workspace_env_hook_path(workspace)


def test_resolve_workspace_env_hook_path_rejects_oversized_script(tmp_path: Path) -> None:
    """Hooks larger than the byte cap are rejected during resolution."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    huge_body = "export FOO=" + ("a" * (sandbox_exec._WORKSPACE_ENV_HOOK_MAX_SCRIPT_BYTES + 8)) + "\n"
    _write_hook(workspace, huge_body)

    with pytest.raises(WorkspaceEnvHookError, match="too large"):
        resolve_workspace_env_hook_path(workspace)


def test_resolve_workspace_env_hook_path_returns_none_for_directory_at_path(tmp_path: Path) -> None:
    """A directory at the hook path is treated as no hook."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".mindroom" / "worker-env.sh").mkdir(parents=True)

    assert resolve_workspace_env_hook_path(workspace) is None


@REQUIRES_BASH
def test_source_workspace_env_hook_captures_exported_values(tmp_path: Path) -> None:
    """Sourced exports become overlay entries."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hook_path = _write_hook(
        workspace,
        "export FOO=bar\nexport NPM_CONFIG_PREFIX=$PWD/.local\n",
    )

    overlay = source_workspace_env_hook(
        hook_path=hook_path,
        base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        cwd=workspace,
    )

    assert overlay["FOO"] == "bar"
    assert overlay["NPM_CONFIG_PREFIX"] == f"{workspace}/.local"


@REQUIRES_BASH
def test_source_workspace_env_hook_skips_non_exported_assignments(tmp_path: Path) -> None:
    """Plain `FOO=bar` assignments without `export` do not persist."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hook_path = _write_hook(workspace, "FOO=bar\nexport BAR=baz\n")

    overlay = source_workspace_env_hook(
        hook_path=hook_path,
        base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        cwd=workspace,
    )

    assert "FOO" not in overlay
    assert overlay["BAR"] == "baz"


@REQUIRES_BASH
def test_source_workspace_env_hook_can_append_to_path(tmp_path: Path) -> None:
    """Hooks may extend PATH using the inherited base PATH."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hook_path = _write_hook(workspace, 'export PATH="$PWD/.local/bin:$PATH"\n')
    base_path = os.environ.get("PATH", "/usr/bin:/bin")

    overlay = source_workspace_env_hook(
        hook_path=hook_path,
        base_env={"PATH": base_path},
        cwd=workspace,
    )

    assert overlay["PATH"].startswith(f"{workspace}/.local/bin:")
    assert overlay["PATH"].endswith(base_path)


@REQUIRES_BASH
def test_source_workspace_env_hook_keeps_user_exported_credentials(tmp_path: Path) -> None:
    """Credential-looking names pass when the hook explicitly exports them."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hook_path = _write_hook(
        workspace,
        "export OPENAI_API_KEY=from-hook\n"
        "export STRIPE_SECRET=from-hook\n"
        "export CI_JOB_TOKEN=from-hook\n"
        "export GITEA_TOKEN=from-hook\n",
    )

    overlay = source_workspace_env_hook(
        hook_path=hook_path,
        base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        cwd=workspace,
    )

    assert overlay["OPENAI_API_KEY"] == "from-hook"
    assert overlay["STRIPE_SECRET"] == "from-hook"  # noqa: S105
    assert overlay["CI_JOB_TOKEN"] == "from-hook"  # noqa: S105
    assert overlay["GITEA_TOKEN"] == "from-hook"  # noqa: S105


@REQUIRES_BASH
def test_source_workspace_env_hook_drops_runner_control_names(tmp_path: Path) -> None:
    """Only runner control names are filtered from the hook overlay."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hook_path = _write_hook(
        workspace,
        "export MINDROOM_SANDBOX_PROXY_TOKEN=leaked\n"
        "export MINDROOM_API_KEY=leaked\n"
        "export MINDROOM_LOCAL_CLIENT_SECRET=leaked\n"
        "export MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH=leaked\n"
        "export MINDROOM_SANDBOX_FOO=leaked\n"
        "export NPM_CONFIG_PREFIX=keep\n",
    )

    overlay = source_workspace_env_hook(
        hook_path=hook_path,
        base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        cwd=workspace,
    )

    assert "MINDROOM_SANDBOX_PROXY_TOKEN" not in overlay
    assert "MINDROOM_API_KEY" not in overlay
    assert "MINDROOM_LOCAL_CLIENT_SECRET" not in overlay
    assert "MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH" not in overlay
    assert "MINDROOM_SANDBOX_FOO" not in overlay
    assert overlay["NPM_CONFIG_PREFIX"] == "keep"


@REQUIRES_BASH
def test_source_workspace_env_hook_drops_transient_bookkeeping(tmp_path: Path) -> None:
    """Bash bookkeeping vars (PWD, OLDPWD, SHLVL, _) are excluded from the overlay."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hook_path = _write_hook(workspace, "export NPM_CONFIG_PREFIX=keep\n")

    overlay = source_workspace_env_hook(
        hook_path=hook_path,
        base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        cwd=workspace,
    )

    assert "PWD" not in overlay
    assert "OLDPWD" not in overlay
    assert "SHLVL" not in overlay
    assert "_" not in overlay


@REQUIRES_BASH
def test_source_workspace_env_hook_skips_unchanged_base_values(tmp_path: Path) -> None:
    """Values identical to the base env do not appear in the overlay."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hook_path = _write_hook(workspace, 'export PATH="$PATH"\nexport FOO=bar\n')
    base_path = os.environ.get("PATH", "/usr/bin:/bin")

    overlay = source_workspace_env_hook(
        hook_path=hook_path,
        base_env={"PATH": base_path},
        cwd=workspace,
    )

    assert "PATH" not in overlay
    assert overlay["FOO"] == "bar"


@REQUIRES_BASH
def test_source_workspace_env_hook_raises_on_non_zero_exit(tmp_path: Path) -> None:
    """A failing hook surfaces as a hook error mentioning the exit code."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hook_path = _write_hook(workspace, 'echo "boom" >&2\nexit 7\n')

    with pytest.raises(WorkspaceEnvHookError, match="exited with code 7"):
        source_workspace_env_hook(
            hook_path=hook_path,
            base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            cwd=workspace,
        )


@REQUIRES_BASH
def test_source_workspace_env_hook_raises_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Subprocess timeout becomes a hook error."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hook_path = _write_hook(workspace, "sleep 1\nexport FOO=bar\n")

    monkeypatch.setattr(sandbox_exec, "_WORKSPACE_ENV_HOOK_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(WorkspaceEnvHookError, match="timed out"):
        source_workspace_env_hook(
            hook_path=hook_path,
            base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            cwd=workspace,
        )


@REQUIRES_BASH
def test_source_workspace_env_hook_rejects_oversized_stderr_on_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Successful hooks cannot write unbounded stderr output."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hook_path = _write_hook(workspace, "python -c \"import sys; sys.stderr.write('x' * 3000)\"\nexport FOO=bar\n")
    monkeypatch.setattr(sandbox_exec, "_WORKSPACE_ENV_HOOK_MAX_OUTPUT_BYTES", 2048)

    with pytest.raises(WorkspaceEnvHookError, match="stderr output"):
        source_workspace_env_hook(
            hook_path=hook_path,
            base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            cwd=workspace,
        )


def test_source_workspace_env_hook_kills_process_group_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Hook timeouts kill the whole child process group."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hook_path = _write_hook(workspace, "sleep 60\n")
    killed_pgids: list[int] = []

    class _TimeoutProcess:
        pid = 12345

        def wait(self, **_kwargs: object) -> int:
            return -9

    def _raise_timeout(_process: object) -> tuple[bytes, bytes]:
        raise subprocess.TimeoutExpired(cmd="bash", timeout=0.01)

    monkeypatch.setattr(sandbox_exec, "_resolve_bash", lambda _base_env: "/fake/bash")
    monkeypatch.setattr(sandbox_exec, "_capture_workspace_env_hook_output", _raise_timeout)
    monkeypatch.setattr(sandbox_exec.subprocess, "Popen", lambda *_args, **_kwargs: _TimeoutProcess())
    monkeypatch.setattr(sandbox_exec.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(sandbox_exec.os, "killpg", lambda pgid, _sig: killed_pgids.append(pgid))

    with pytest.raises(WorkspaceEnvHookError, match="timed out"):
        source_workspace_env_hook(
            hook_path=hook_path,
            base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            cwd=workspace,
        )

    assert killed_pgids == [12345]


@REQUIRES_BASH
def test_source_workspace_env_hook_rejects_overlay_that_exceeds_total_cap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The total overlay byte cap rejects the first entry that would exceed it."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hook_path = _write_hook(workspace, "export FIRST=12345\nexport SECOND=12345\n")
    monkeypatch.setattr(sandbox_exec, "_WORKSPACE_ENV_HOOK_MAX_OVERLAY_BYTES", len("FIRST=12345"))

    with pytest.raises(WorkspaceEnvHookError, match="overlay is too large"):
        source_workspace_env_hook(
            hook_path=hook_path,
            base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            cwd=workspace,
        )


def test_source_workspace_env_hook_raises_when_bash_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Missing bash surfaces as a hook error rather than a crash."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hook_path = _write_hook(workspace, "export FOO=bar\n")

    monkeypatch.setattr(sandbox_exec.shutil, "which", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sandbox_exec.os, "access", lambda *_args, **_kwargs: False)

    with pytest.raises(WorkspaceEnvHookError, match="bash is required"):
        source_workspace_env_hook(
            hook_path=hook_path,
            base_env={},
            cwd=workspace,
        )


@REQUIRES_BASH
def test_source_workspace_env_hook_ignores_script_stdout_before_marker(tmp_path: Path) -> None:
    """Script stdout printed before the env capture marker is ignored."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hook_path = _write_hook(workspace, 'echo "noisy script output"\nexport FOO=bar\n')

    overlay = source_workspace_env_hook(
        hook_path=hook_path,
        base_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        cwd=workspace,
    )

    assert overlay["FOO"] == "bar"


def test_workspace_env_hook_relative_path_value() -> None:
    """The exposed relative path constant matches the documented filename."""
    assert Path(".mindroom") / "worker-env.sh" == _WORKSPACE_ENV_HOOK_RELATIVE_PATH
