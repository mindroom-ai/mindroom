"""Tests for async shell tool with timeout-to-handle support."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from mindroom.constants import RuntimePaths, resolve_runtime_paths, workspace_home_identity_env
from mindroom.shell.execution import _MAX_OUTPUT_BYTES
from mindroom.tool_jobs.control import JobControl, job_control_context
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.progress import tool_progress_reporter
from mindroom.tools.shell import (
    _shell_subprocess_env,
    _workspace_home_contract_env_from_process_env,
    shell_tools,
)

if TYPE_CHECKING:
    from pathlib import Path

    from agno.tools.function import Function
    from agno.tools.toolkit import Toolkit

    from mindroom.constants import RuntimePaths


def _make_runtime_paths(tmp_path: Path, *, process_env: dict[str, str] | None = None) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={} if process_env is None else process_env,
    )


def _get_toolkit(tmp_path: Path) -> Toolkit:
    runtime_paths = _make_runtime_paths(tmp_path)
    return get_tool_by_name("shell", runtime_paths, disable_sandbox_proxy=True, worker_target=None)


def _fork_child_holding_stdio_script(*, parent_stderr: str = "", parent_exit_code: int = 0) -> str:
    return (
        "import os\n"
        "import pathlib\n"
        "import signal\n"
        "import sys\n"
        "import time\n"
        "stop_file = pathlib.Path(sys.argv[2])\n"
        "def stop_child(signum, frame):\n"
        "    stop_file.write_text('stopped', encoding='utf-8')\n"
        "    os._exit(0)\n"
        f"sys.stderr.write({parent_stderr!r})\n"
        "sys.stderr.flush()\n"
        "pid = os.fork()\n"
        "if pid:\n"
        "    pathlib.Path(sys.argv[1]).write_text(str(pid), encoding='utf-8')\n"
        f"    sys.exit({parent_exit_code})\n"
        "signal.signal(signal.SIGTERM, stop_child)\n"
        "time.sleep(30)\n"
    )


async def _wait_for_pid_file(pid_file: Path) -> int:
    last_text: str | None = None
    for _ in range(50):
        if pid_file.exists():
            last_text = pid_file.read_text(encoding="utf-8").strip()
            if last_text:
                return int(last_text)
        await asyncio.sleep(0.05)
    message = f"PID file was not written: {pid_file}" if last_text is None else f"PID file was empty: {pid_file}"
    raise AssertionError(message)


async def _stop_recorded_child(pid: int, stop_file: Path) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)

    for _ in range(20):
        if stop_file.exists():
            return
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.05)

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)

    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.05)

    message = f"Child process {pid} did not stop"
    raise AssertionError(message)


def _get_run_shell_command_function(tool: Toolkit) -> Function:
    return tool.async_functions["run_shell_command"]


# ---------------------------------------------------------------------------
# run_shell_command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_shell_command_returns_output(tmp_path: Path) -> None:
    """Fast command should return stdout directly."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(["echo", "hello world"])
    assert result == "hello world"


@pytest.mark.asyncio
async def test_run_shell_command_bash_echo_returns_output(tmp_path: Path) -> None:
    """Normal shell output should still be returned."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(["bash", "-lc", "echo hello"])
    assert result == "hello"


@pytest.mark.asyncio
async def test_run_shell_command_accepts_shell_command_string(tmp_path: Path) -> None:
    """Natural shell command strings should execute through bash."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint("echo $HOME")
    assert result
    assert not result.startswith("Error:")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "if shopt -q login_shell; then echo login; else echo plain; fi",
        ["if shopt -q login_shell; then echo login; else echo plain; fi"],
    ],
)
async def test_implicit_shell_does_not_run_login_startup(tmp_path: Path, command: str | list[str]) -> None:
    """Ordinary commands must not pay for or acquire side effects from login profiles."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None
    assert await entrypoint(command) == "plain"


@pytest.mark.asyncio
async def test_explicit_login_shell_remains_available(tmp_path: Path) -> None:
    """Callers opting into login initialization still get it."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None
    assert await entrypoint(["bash", "-lc", "shopt -q login_shell && echo login"]) == "login"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("[ 1 -eq 1 ] && echo ok", "ok"),
        ("{ echo ok; }", "ok"),
    ],
)
async def test_run_shell_command_accepts_bracketed_shell_command_strings(
    tmp_path: Path,
    command: str,
    expected: str,
) -> None:
    """Shell grammar that starts with JSON-like characters should still execute through bash."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(command)
    assert result == expected


@pytest.mark.asyncio
async def test_run_shell_command_accepts_single_item_shell_command_line(tmp_path: Path) -> None:
    """LLMs often send one shell command line inside the argv array."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(["printf '%s' ok"])
    assert result == "ok"


def test_run_shell_command_schema_accepts_string_or_string_array(tmp_path: Path) -> None:
    """The tool schema should expose both natural command strings and argv lists."""
    tool = _get_toolkit(tmp_path)
    args_schema = _get_run_shell_command_function(tool).parameters["properties"]["args"]

    assert args_schema["anyOf"] == [
        {"type": "array", "items": {"type": "string"}},
        {"type": "string"},
    ]


@pytest.mark.asyncio
async def test_run_shell_command_parses_json_string_args(tmp_path: Path) -> None:
    """JSON-stringified args should be parsed into a shell argv list."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint('["echo", "hello world"]')
    assert result == "hello world"


@pytest.mark.asyncio
async def test_run_shell_command_parses_single_item_json_args(tmp_path: Path) -> None:
    """A single command item in JSON string form should execute normally."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint('["ls"]')
    assert result
    assert not result.startswith("Error:")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        '["echo",',
        '{"cmd": "ls"}',
        "",
        '["echo", 42]',
        '["bash", ["-c", "ls"]]',
        '["echo", {"x": 1}]',
    ],
)
async def test_run_shell_command_rejects_invalid_stringified_args(tmp_path: Path, args: str) -> None:
    """Malformed or non-flat stringified args should fail validation."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(args)
    assert result.startswith("Error:")
    assert "'args' must be a shell command string or a flat list of strings" in result


@pytest.mark.asyncio
async def test_run_shell_command_empty_json_list_uses_existing_empty_behavior(tmp_path: Path) -> None:
    """An empty JSON list should fall through to the existing subprocess error path."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint("[]")
    assert result.startswith("Error:")


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ["", "   "])
async def test_run_shell_command_entrypoint_returns_error_for_empty_string_args(tmp_path: Path, args: str) -> None:
    """Direct entrypoint calls should return validation errors instead of raising."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(args)
    assert result.startswith("Error:")
    assert "'args' must be a shell command string or a flat list of strings" in result


@pytest.mark.asyncio
async def test_run_shell_command_returns_error_on_nonzero_exit(tmp_path: Path) -> None:
    """Non-zero exit should return stderr."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(["bash", "-c", "echo oops >&2; exit 1"])
    assert result.startswith("Error:")
    assert "oops" in result


@pytest.mark.asyncio
async def test_run_shell_command_preserves_stdout_receipt_on_nonzero_exit(tmp_path: Path) -> None:
    """A nonterminal CLI receipt on stdout must retain its continuation handle."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(["bash", "-c", 'printf \'{"status":"queued","call_id":"call-1"}\'; exit 3'])

    assert result == 'Error: {"status":"queued","call_id":"call-1"}'


@pytest.mark.asyncio
async def test_run_shell_command_preserves_both_failure_streams(tmp_path: Path) -> None:
    """Useful stdout must not displace the existing stderr failure detail."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(["bash", "-c", "printf receipt; printf warning >&2; exit 3"])

    assert result == "Error: receipt\nStderr:\nwarning"


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
async def test_run_shell_command_returns_after_foreground_exit_with_inherited_pipe_child(tmp_path: Path) -> None:
    """Regression: tmux/auth helpers/watchers/daemons can inherit pipes after foreground exit."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    pid_file = tmp_path / "child.pid"
    stop_file = tmp_path / "child.stopped"
    command = [sys.executable, "-c", _fork_child_holding_stdio_script(), str(pid_file), str(stop_file)]
    started = time.perf_counter()
    task = asyncio.create_task(entrypoint(command, timeout=10))
    child_pid: int | None = None

    try:
        child_pid = await _wait_for_pid_file(pid_file)
        result = await asyncio.wait_for(task, timeout=2)
    finally:
        if child_pid is not None:
            await _stop_recorded_child(child_pid, stop_file)
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    elapsed = time.perf_counter() - started
    assert elapsed < 2
    assert result == ""


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
async def test_run_shell_command_nonzero_foreground_exit_with_inherited_pipe_child(tmp_path: Path) -> None:
    """Nonzero foreground exit should return buffered stderr without waiting for descendant EOF."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    pid_file = tmp_path / "child-error.pid"
    stop_file = tmp_path / "child-error.stopped"
    command = [
        sys.executable,
        "-c",
        _fork_child_holding_stdio_script(parent_stderr="foreground failed\n", parent_exit_code=7),
        str(pid_file),
        str(stop_file),
    ]
    started = time.perf_counter()
    task = asyncio.create_task(entrypoint(command, timeout=10))
    child_pid: int | None = None

    try:
        child_pid = await _wait_for_pid_file(pid_file)
        result = await asyncio.wait_for(task, timeout=2)
    finally:
        if child_pid is not None:
            await _stop_recorded_child(child_pid, stop_file)
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    elapsed = time.perf_counter() - started
    assert elapsed < 2
    assert result == "Error: foreground failed"


@pytest.mark.asyncio
async def test_run_shell_command_tail_parameter(tmp_path: Path) -> None:
    """Only the last N lines should be returned."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(["bash", "-c", "for i in $(seq 1 20); do echo line$i; done"], tail=3)
    lines = result.strip().split("\n")
    assert lines == ["line18", "line19", "line20"]


@pytest.mark.asyncio
async def test_run_shell_command_truncates_large_output_by_bytes(tmp_path: Path) -> None:
    """A small line count with very large lines should not return an unbounded result."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    script = "for i in range(120): print(f'{i:03d}:' + 'x' * 1000)"
    result = await entrypoint([sys.executable, "-c", script], tail=120)

    assert "Output truncated to the last" in result
    assert "119:" in result
    assert "000:" not in result
    assert len(result.encode("utf-8")) <= _MAX_OUTPUT_BYTES + 200


@pytest.mark.asyncio
async def test_run_shell_command_truncates_large_stderr_by_bytes(tmp_path: Path) -> None:
    """Large stderr from failed commands should be byte-capped too."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    script = "import sys\nfor i in range(120): print(f'{i:03d}:' + 'x' * 1000, file=sys.stderr)\nsys.exit(1)\n"
    result = await entrypoint([sys.executable, "-c", script])

    assert result.startswith("Error: [Output truncated to the last")
    assert "119:" in result
    assert "000:" not in result
    assert len(result.encode("utf-8")) <= _MAX_OUTPUT_BYTES + 200


@pytest.mark.asyncio
async def test_run_shell_command_truncates_many_short_lines_by_rendered_bytes(tmp_path: Path) -> None:
    """Rendered newline separators should be included in the output byte cap."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    script = "for _ in range(10000): print('xxxxxx')"
    result = await entrypoint([sys.executable, "-c", script], tail=10000)

    assert result.startswith("[Output truncated to the last")
    assert len(result.encode("utf-8")) <= _MAX_OUTPUT_BYTES + 200


@pytest.mark.asyncio
async def test_run_shell_command_truncates_oversized_single_stdout_line(tmp_path: Path) -> None:
    """Output without newlines should still be retained and byte-capped."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint([sys.executable, "-c", "import sys; sys.stdout.write('A' * 100000)"])

    assert result.startswith("[Output truncated to the last")
    assert "A" * 100 in result
    assert len(result.encode("utf-8")) <= _MAX_OUTPUT_BYTES + 200


@pytest.mark.asyncio
async def test_run_shell_command_truncates_oversized_single_stderr_line(tmp_path: Path) -> None:
    """Oversized stderr lines should be surfaced instead of skipped."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(
        [sys.executable, "-c", "import sys; sys.stderr.write('B' * 100000); sys.exit(1)"],
    )

    assert result.startswith("Error: [Output truncated to the last")
    assert "B" * 100 in result
    assert len(result.encode("utf-8")) <= _MAX_OUTPUT_BYTES + 200


@pytest.mark.asyncio
async def test_run_shell_command_error_on_bad_command(tmp_path: Path) -> None:
    """Non-existent command should return an error."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(["__nonexistent_command_xyz__"])
    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_run_shell_command_respects_base_dir(tmp_path: Path) -> None:
    """Shell should execute in base_dir when configured."""
    runtime_paths = _make_runtime_paths(tmp_path)
    tool = get_tool_by_name(
        "shell",
        runtime_paths,
        disable_sandbox_proxy=True,
        worker_target=None,
        tool_init_overrides={"base_dir": str(tmp_path)},
    )
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(["pwd"])
    assert result.splitlines() == [f"[cwd: {tmp_path}]", str(tmp_path)]


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def test_shell_registers_only_run_shell_command(tmp_path: Path) -> None:
    """A job is how a long command runs in the background, so the shell has no handle functions."""
    tool = _get_toolkit(tmp_path)

    assert list(tool.async_functions) == ["run_shell_command"]
    assert tool.functions == {}


def test_shell_disabled_registers_no_tools(tmp_path: Path) -> None:
    """Shell toolkit with enable_run_shell_command=False should have no tools."""
    runtime_paths = _make_runtime_paths(tmp_path)

    cls = shell_tools()
    toolkit = cls(enable_run_shell_command=False, runtime_paths=runtime_paths)
    assert len(toolkit.functions) == 0
    assert len(toolkit.async_functions) == 0


# ---------------------------------------------------------------------------
# Env passthrough
# ---------------------------------------------------------------------------


def test_workspace_home_identity_env_builds_shell_identity_fragment(tmp_path: Path) -> None:
    """The shared workspace HOME fragment should use stable workspace-relative XDG paths."""
    workspace = tmp_path / "workspace"

    assert workspace_home_identity_env(workspace) == {
        "HOME": str(workspace),
        "MINDROOM_AGENT_WORKSPACE": str(workspace),
        "XDG_CONFIG_HOME": str(workspace / ".config"),
        "XDG_DATA_HOME": str(workspace / ".local" / "share"),
        "XDG_STATE_HOME": str(workspace / ".local" / "state"),
    }


def test_workspace_home_contract_env_requires_full_identity_fragment(tmp_path: Path) -> None:
    """Shell subprocesses should forward the workspace contract only when identity is coherent."""
    workspace = tmp_path / "workspace"
    base_env = {
        **workspace_home_identity_env(workspace),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "PIP_CACHE_DIR": str(tmp_path / "cache" / "pip"),
        "UV_CACHE_DIR": str(tmp_path / "cache" / "uv"),
        "PYTHONPYCACHEPREFIX": str(tmp_path / "cache" / "pycache"),
        "VIRTUAL_ENV": str(tmp_path / "venv"),
    }

    assert _workspace_home_contract_env_from_process_env(base_env) == base_env

    mismatched_env = dict(base_env)
    mismatched_env["XDG_DATA_HOME"] = str(tmp_path / "other-data")
    assert _workspace_home_contract_env_from_process_env(mismatched_env) == {}


def test_shell_subprocess_env_exports_workspace_from_base_dir_without_home_contract(tmp_path: Path) -> None:
    """Local execution keeps the host HOME but still exports the workspace env var."""
    workspace = tmp_path / "workspace"

    env = _shell_subprocess_env({}, base_process_env={"HOME": "/home/host-user"}, workspace_dir=workspace)

    assert env["MINDROOM_AGENT_WORKSPACE"] == str(workspace.resolve())
    assert env["HOME"] == "/home/host-user"


def test_shell_subprocess_env_replaces_incomplete_workspace_contract(tmp_path: Path) -> None:
    """An unvalidated process workspace must not override the local agent workspace."""
    workspace = tmp_path / "workspace"
    stale_workspace = tmp_path / "stale-workspace"
    base_env = {
        "HOME": "/home/host-user",
        "MINDROOM_AGENT_WORKSPACE": str(stale_workspace),
    }

    env = _shell_subprocess_env(
        {"MINDROOM_AGENT_WORKSPACE": str(stale_workspace)},
        base_process_env=base_env,
        workspace_dir=workspace,
    )

    assert env["MINDROOM_AGENT_WORKSPACE"] == str(workspace.resolve())
    assert env["HOME"] == "/home/host-user"


def test_shell_subprocess_env_prefers_worker_home_contract_over_base_dir(tmp_path: Path) -> None:
    """The worker workspace HOME contract stays authoritative over the base_dir fallback."""
    contract_workspace = tmp_path / "contract-workspace"
    base_env = {
        **workspace_home_identity_env(contract_workspace),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "PIP_CACHE_DIR": str(tmp_path / "cache" / "pip"),
        "UV_CACHE_DIR": str(tmp_path / "cache" / "uv"),
        "PYTHONPYCACHEPREFIX": str(tmp_path / "cache" / "pycache"),
        "VIRTUAL_ENV": str(tmp_path / "venv"),
    }

    env = _shell_subprocess_env({}, base_process_env=base_env, workspace_dir=tmp_path / "other-base-dir")

    assert env["MINDROOM_AGENT_WORKSPACE"] == str(contract_workspace)
    assert env["HOME"] == str(contract_workspace)


@pytest.mark.asyncio
async def test_local_shell_replaces_stale_agent_workspace_env(tmp_path: Path) -> None:
    """Local shell execution should replace a stale process workspace with base_dir."""
    workspace = tmp_path / "agent-workspace"
    workspace.mkdir()
    runtime_paths = _make_runtime_paths(
        tmp_path,
        process_env={
            "HOME": str(tmp_path / "host-home"),
            "MINDROOM_AGENT_WORKSPACE": str(tmp_path / "stale-workspace"),
        },
    )
    tool = get_tool_by_name(
        "shell",
        runtime_paths,
        disable_sandbox_proxy=True,
        worker_target=None,
        tool_init_overrides={"base_dir": str(workspace)},
    )
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint('printf %s "$MINDROOM_AGENT_WORKSPACE"')
    assert result.splitlines() == [f"[cwd: {workspace}]", str(workspace.resolve())]


def test_run_shell_command_description_uses_portable_workspace_paths(tmp_path: Path) -> None:
    """The shell description should give workspace guidance valid in every execution mode."""
    workspace = tmp_path / "workspace"
    runtime_paths = _make_runtime_paths(tmp_path, process_env={"HOME": "/home/host-user"})
    tool = get_tool_by_name(
        "shell",
        runtime_paths,
        disable_sandbox_proxy=True,
        worker_target=None,
        tool_init_overrides={"base_dir": str(workspace)},
    )

    description = tool.async_functions["run_shell_command"].description
    assert description is not None
    assert "Always use relative paths" in description
    assert "$MINDROOM_AGENT_WORKSPACE" in description
    assert "worker-routed execution maps `~` to the workspace" in description
    assert "local execution maps it to the host home" in description
    assert description.count("Working method:") == 1


def test_proxied_run_shell_command_description_does_not_claim_host_home(tmp_path: Path) -> None:
    """The model-facing proxy description must not misidentify worker execution as local."""
    workspace = tmp_path / "workspace"
    runtime_paths = _make_runtime_paths(
        tmp_path,
        process_env={
            "HOME": "/home/host-user",
            "MINDROOM_SANDBOX_EXECUTION_MODE": "all",
        },
    )
    tool = get_tool_by_name(
        "shell",
        runtime_paths,
        worker_target=None,
        tool_init_overrides={"base_dir": str(workspace)},
    )

    description = tool.async_functions["run_shell_command"].description
    assert description is not None
    assert "worker-routed execution maps `~` to the workspace" in description
    assert "`~` and `$HOME` point at the host home" not in description


def test_run_shell_command_description_has_no_workspace_note_without_base_dir(tmp_path: Path) -> None:
    """Without a workspace there is no cwd contract to describe, but the working method still applies."""
    tool = _get_toolkit(tmp_path)

    description = tool.async_functions["run_shell_command"].description
    assert description is not None
    assert "[cwd:" not in description
    assert description.count("Working method:") == 1


@pytest.mark.asyncio
async def test_env_passthrough_preserved(tmp_path: Path) -> None:
    """Runtime env values from .env should be visible in shell commands."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("MY_TEST_VAR=async-shell-works\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )
    tool = get_tool_by_name("shell", runtime_paths, disable_sandbox_proxy=True, worker_target=None)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(["bash", "-lc", "printf '%s' \"$MY_TEST_VAR\""])
    assert result == "async-shell-works"


@pytest.mark.asyncio
async def test_login_bash_preserves_runtime_path_after_profile_reset(tmp_path: Path) -> None:
    """MindRoom shell calls should keep runtime PATH even when login startup rewrites it."""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_bash = fake_bin / "bash"
    fake_bash.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-lc" ]; then\n'
        "  export PATH=/profile/default\n"
        '  exec /bin/sh -c "$2"\n'
        "fi\n"
        'exec /bin/sh "$@"\n',
        encoding="utf-8",
    )
    fake_bash.chmod(0o755)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={"PATH": "/worker-env/bin:/usr/bin:/bin"},
    )
    tool = get_tool_by_name("shell", runtime_paths, disable_sandbox_proxy=True, worker_target=None)
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint([str(fake_bash), "-lc", "printf '%s' \"$PATH\""])

    assert result == "/worker-env/bin:/usr/bin:/bin"


@pytest.mark.asyncio
async def test_run_shell_command_stops_a_command_past_its_timeout(tmp_path: Path) -> None:
    """A command past its timeout is stopped, and the call returns its output so far without a handle."""
    runtime_paths = _make_runtime_paths(tmp_path)
    tool = get_tool_by_name(
        "shell",
        runtime_paths,
        disable_sandbox_proxy=True,
        worker_target=None,
        tool_init_overrides={"base_dir": str(tmp_path)},
    )
    entrypoint = tool.async_functions["run_shell_command"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(["bash", "-c", "echo started; sleep 300"], timeout=1)

    assert result == f"[cwd: {tmp_path}]\nStopped after 1s.\nOutput:\nstarted"


@pytest.mark.asyncio
async def test_only_a_job_command_runs_past_the_default_time_limit(tmp_path: Path) -> None:
    """Without a timeout, a call that runs as a background job has no limit and any other call has the default."""
    entrypoint = _get_run_shell_command_function(_get_toolkit(tmp_path)).entrypoint
    assert entrypoint is not None
    command = ["bash", "-c", "sleep 0.6; echo done"]

    with patch("mindroom.shell.execution._DEFAULT_RUN_TIMEOUT_SECONDS", 0.2):
        stopped = await entrypoint(command)
        with job_control_context(JobControl()):
            finished = await entrypoint(command)

    assert stopped == "Stopped after 0.2s."
    assert finished == "done"


@pytest.mark.asyncio
async def test_a_shell_call_reports_its_output_as_its_progress(tmp_path: Path) -> None:
    """A shell call's output tail reaches whoever owns the call, such as its job, while the command runs."""
    entrypoint = _get_run_shell_command_function(_get_toolkit(tmp_path)).entrypoint
    assert entrypoint is not None
    reports: list[str] = []

    with patch("mindroom.shell.execution._PROGRESS_INTERVAL_SECONDS", 0.1), tool_progress_reporter(reports.append):
        result = await entrypoint(["bash", "-c", "echo building; sleep 0.5; echo built"])

    assert result == "building\nbuilt"
    assert reports[0] == "building"


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [0, -5])
async def test_a_timeout_that_is_not_positive_is_rejected_before_the_command_runs(tmp_path: Path, seconds: int) -> None:
    """A zero or negative timeout would stop the command at once, so the call explains the argument instead."""
    entrypoint = _get_run_shell_command_function(_get_toolkit(tmp_path)).entrypoint
    assert entrypoint is not None
    marker = tmp_path / "ran"

    result = await entrypoint(["touch", str(marker)], timeout=seconds)

    assert result == "Error: timeout must be a positive number of seconds; omit it for the default limit."
    assert not marker.exists()
