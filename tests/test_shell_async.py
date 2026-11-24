"""Tests for async shell execution and timeout termination."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths, workspace_home_identity_env
from mindroom.shell.background_scope import (
    BackgroundLogPolicy,
    ShellBackgroundScope,
    open_background_log,
    shell_background_scope,
)
from mindroom.shell.execution import (
    _MAX_OUTPUT_BYTES,
    ProcessRecord,
    _background_rejection,
    _format_auto_background_message,
    _OutputBuffer,
    _OutputProgress,
    _progress_lines,
    kill_namespace_records,
    run_command,
    shell_request_budget_seconds,
)
from mindroom.tool_jobs.control import JobControl, job_control_context
from mindroom.tool_system.agno_toolkits import toolkit_function
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.output_files import (
    ToolOutputFileHandled,
    ToolOutputFilePolicy,
    _active_output_request,
    prepare_tool_output_file,
    with_output_files,
)
from mindroom.tool_system.progress import shown_progress, tool_progress_reporter
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.tool_environment import ToolEnvironment
from mindroom.tools.shell import (
    _shell_subprocess_env,
    _workspace_home_contract_env_from_process_env,
    shell_tools,
)
from tests.process_helpers import assert_linux_pid_not_running

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.agno_toolkits import AnyToolkit
    from mindroom.tool_system.tool_function import ToolFunction


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


def _get_toolkit(tmp_path: Path) -> AnyToolkit:
    runtime_paths = _make_runtime_paths(tmp_path)
    return get_tool_by_name(
        "shell",
        runtime_paths,
        runs_in_worker=False,
        worker_target=None,
        tools=builtin_tool_registry(),
    )


def _shell_for_config(tmp_path: Path, defaults: dict[str, object]) -> AnyToolkit:
    """Build the shell for a config with these ``defaults``, placed where the sandbox settings place it."""
    runtime_paths = _make_runtime_paths(tmp_path)
    config = Config.validate_with_runtime({"defaults": defaults}, runtime_paths)
    return get_tool_by_name(
        "shell",
        runtime_paths,
        runtime_config=config,
        worker_target=None,
        tools=builtin_tool_registry(),
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_shell_refusal")
@pytest.mark.parametrize("defaults", [{}, {"allow_shell_without_worker": False}], ids=["unset", "off"])
async def test_shell_without_a_worker_refuses_each_command(tmp_path: Path, defaults: dict[str, object]) -> None:
    """Without a worker, and without the setting that allows it, a command never runs in MindRoom's process."""
    marker = tmp_path / "ran"
    tool = _shell_for_config(tmp_path, defaults)

    result = await tool.functions["run_shell_command"](["touch", str(marker)])

    assert result == (
        "Error: Shell commands run only in a worker, and this agent's shell runs in MindRoom's own process. "
        "Route `shell` to a worker, or set `defaults.allow_shell_without_worker: true` in config.yaml to run them here."
    )
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_shell_refusal")
async def test_shell_without_a_worker_runs_commands_when_the_config_allows_it(tmp_path: Path) -> None:
    """`defaults.allow_shell_without_worker: true` lets the shell run commands in MindRoom's process."""
    marker = tmp_path / "ran"
    tool = _shell_for_config(tmp_path, {"allow_shell_without_worker": True})

    result = await tool.functions["run_shell_command"](f"touch {marker} && echo ran-here")

    assert result == "ran-here"
    assert marker.exists()


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


def _get_run_shell_command_function(tool: AnyToolkit) -> ToolFunction:
    function = toolkit_function(tool, "run_shell_command")
    assert function is not None
    return function


# ---------------------------------------------------------------------------
# run_shell_command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_shell_command_returns_output(tmp_path: Path) -> None:
    """Fast command should return stdout directly."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.functions["run_shell_command"]

    result = await entrypoint(["echo", "hello world"])
    assert result == "hello world"


@pytest.mark.asyncio
async def test_run_shell_command_bash_echo_returns_output(tmp_path: Path) -> None:
    """Normal shell output should still be returned."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.functions["run_shell_command"]

    result = await entrypoint(["bash", "-lc", "echo hello"])
    assert result == "hello"


@pytest.mark.asyncio
async def test_run_shell_command_accepts_shell_command_string(tmp_path: Path) -> None:
    """Natural shell command strings should execute through bash."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.functions["run_shell_command"]

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
    entrypoint = tool.functions["run_shell_command"]
    assert await entrypoint(command) == "plain"


@pytest.mark.asyncio
async def test_explicit_login_shell_remains_available(tmp_path: Path) -> None:
    """Callers opting into login initialization still get it."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.functions["run_shell_command"]
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
    entrypoint = tool.functions["run_shell_command"]

    result = await entrypoint(command)
    assert result == expected


@pytest.mark.asyncio
async def test_run_shell_command_accepts_single_item_shell_command_line(tmp_path: Path) -> None:
    """LLMs often send one shell command line inside the argv array."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.functions["run_shell_command"]

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
    entrypoint = tool.functions["run_shell_command"]

    result = await entrypoint('["echo", "hello world"]')
    assert result == "hello world"


@pytest.mark.asyncio
async def test_run_shell_command_parses_single_item_json_args(tmp_path: Path) -> None:
    """A single command item in JSON string form should execute normally."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.functions["run_shell_command"]

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
        "   ",
        '["echo", 42]',
        '["bash", ["-c", "ls"]]',
        '["echo", {"x": 1}]',
    ],
)
async def test_run_shell_command_rejects_invalid_stringified_args(tmp_path: Path, args: str) -> None:
    """Malformed, blank, or non-flat stringified args return a validation error instead of raising."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.functions["run_shell_command"]

    result = await entrypoint(args)
    assert result.startswith("Error:")
    assert "'args' must be a shell command string or a flat list of strings" in result


@pytest.mark.asyncio
async def test_run_shell_command_empty_json_list_uses_existing_empty_behavior(tmp_path: Path) -> None:
    """An empty JSON list should fall through to the existing subprocess error path."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.functions["run_shell_command"]

    result = await entrypoint("[]")
    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_run_shell_command_returns_error_on_nonzero_exit(tmp_path: Path) -> None:
    """Non-zero exit should return its exit code and stderr."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.functions["run_shell_command"]

    result = await entrypoint(["bash", "-c", "echo oops >&2; exit 1"])
    assert result == "Exit code 1\nStderr:\noops"


@pytest.mark.asyncio
async def test_run_shell_command_preserves_stdout_receipt_on_nonzero_exit(tmp_path: Path) -> None:
    """A nonterminal CLI receipt on stdout must retain its continuation handle."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.functions["run_shell_command"]

    result = await entrypoint(["bash", "-c", 'printf \'{"status":"queued","call_id":"call-1"}\'; exit 3'])

    assert result == 'Exit code 3\nOutput:\n{"status":"queued","call_id":"call-1"}'


@pytest.mark.asyncio
async def test_run_shell_command_preserves_both_failure_streams(tmp_path: Path) -> None:
    """Useful stdout must not displace the existing stderr failure detail."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.functions["run_shell_command"]

    result = await entrypoint(["bash", "-c", "printf receipt; printf warning >&2; exit 3"])

    assert result == "Exit code 3\nOutput:\nreceipt\nStderr:\nwarning"


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
async def test_run_shell_command_returns_after_foreground_exit_with_inherited_pipe_child(tmp_path: Path) -> None:
    """Regression: tmux/auth helpers/watchers/daemons can inherit pipes after foreground exit."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.functions["run_shell_command"]

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
    entrypoint = tool.functions["run_shell_command"]

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
    assert result == "Exit code 7\nStderr:\nforeground failed"


@pytest.mark.asyncio
async def test_run_shell_command_tail_parameter(tmp_path: Path) -> None:
    """Only the last N lines are returned, after a notice of how many it cut; uncut output has no notice."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.functions["run_shell_command"]

    result = await entrypoint("seq 1 20", tail=3)
    assert result.splitlines() == [
        "[Showing the last 3 of 20 lines. Pass a larger tail, or narrow the command.]",
        "18",
        "19",
        "20",
    ]
    assert await entrypoint("seq 1 20", tail=20) == "\n".join(str(number) for number in range(1, 21))


@pytest.mark.asyncio
async def test_run_shell_command_keeps_stderr_on_success(tmp_path: Path) -> None:
    """A command that succeeds returns its stderr after its stdout, under the same tail rule."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.functions["run_shell_command"]

    assert await entrypoint("echo out; echo warn >&2") == "out\nStderr:\nwarn"
    # The notice of a cut leads the result even when only stderr was cut, and names the stream.
    assert await entrypoint("seq 1 5 >&2", tail=2) == (
        "[Showing the last 2 of 5 stderr lines. Pass a larger tail, or narrow the command.]\nStderr:\n4\n5"
    )


def test_a_tail_cut_of_byte_capped_output_gives_one_notice() -> None:
    """Output cut by both the byte cap and ``tail`` says so in one notice, counting every line printed."""
    buffer = _OutputBuffer(max_bytes=12)
    buffer.append("1\n2\n3\n4\n5\n6\n7\n8\n")

    assert buffer.render(tail=2) == (
        "[Showing the last 2 of 8 lines; output truncated to the last 12 bytes. "
        "Redirect command output to a file for complete results.]\n7\n8"
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_a_notice_counts_blank_lines_and_a_missing_final_newline_alike(newline: str) -> None:
    """A blank last line counts as a line with LF as with CRLF; uncut output keeps its old form."""
    buffer = _OutputBuffer()
    buffer.append(f"a{newline}b{newline}{newline}")
    unterminated = _OutputBuffer()
    unterminated.append(f"a{newline}b")

    # The one line shown is the blank one, which holds a carriage return when the command ended lines with CRLF.
    assert buffer.render(tail=1) == "[Showing the last 1 of 3 lines. Pass a larger tail, or narrow the command.]" + (
        "\n\r" if newline == "\r\n" else ""
    )
    assert len(buffer) == 3
    assert buffer.render() == f"a{newline}b{newline}{newline}".rstrip("\n")
    assert unterminated.render(tail=1).splitlines() == [
        "[Showing the last 1 of 2 lines. Pass a larger tail, or narrow the command.]",
        "b",
    ]


@pytest.mark.asyncio
async def test_run_shell_command_keeps_blank_stderr_and_trims_only_whole_stdout(tmp_path: Path) -> None:
    """Stderr that holds only blank lines keeps its section, with LF as with CRLF; whole stdout is as it always was."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.functions["run_shell_command"]
    notice = "[Showing the last 1 of 2 stderr lines. Pass a larger tail, or narrow the command.]"

    assert await entrypoint("echo out; echo >&2") == "out\nStderr:\n"
    assert await entrypoint("echo >&2; exit 1") == "Exit code 1\nStderr:\n"
    assert await entrypoint(r"printf 'a\n\n' >&2", tail=1) == f"{notice}\nStderr:\n"
    assert await entrypoint(r"printf 'a\r\n\r\n' >&2", tail=1) == f"{notice}\nStderr:\n\r"
    assert await entrypoint(r"printf 'a\n\n\n'") == "a"
    assert await entrypoint("echo") == ""


def test_the_line_cap_keeps_line_boundaries_across_appends() -> None:
    """Trimming to the line cap keeps the newline before the next append, and the notice names the cap."""
    buffer = _OutputBuffer(max_lines=2)
    buffer.append("1\n2\n3\n")
    buffer.append("4\n")

    assert buffer.render() == (
        "[Showing the last 2 of 4 lines; output truncated to the last 2 lines. "
        "Redirect command output to a file for complete results.]\n3\n4"
    )


@pytest.mark.asyncio
async def test_run_shell_command_truncates_large_output_by_bytes(tmp_path: Path) -> None:
    """A small line count with very large lines should not return an unbounded result."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.functions["run_shell_command"]

    script = "for i in range(120): print(f'{i:03d}:' + 'x' * 1000)"
    result = await entrypoint([sys.executable, "-c", script], tail=120)

    assert result.startswith("[Showing the last ")
    assert " of 120 lines; output truncated to the last 51200 bytes." in result
    assert "119:" in result
    assert "000:" not in result
    assert len(result.encode("utf-8")) <= _MAX_OUTPUT_BYTES + 200


@pytest.mark.asyncio
async def test_run_shell_command_truncates_large_stderr_by_bytes(tmp_path: Path) -> None:
    """Large stderr from failed commands should be byte-capped too."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.functions["run_shell_command"]

    script = "import sys\nfor i in range(120): print(f'{i:03d}:' + 'x' * 1000, file=sys.stderr)\nsys.exit(1)\n"
    result = await entrypoint([sys.executable, "-c", script])

    assert result.startswith("Exit code 1\nStderr:\n[Showing the last ")
    assert " of 120 lines; output truncated to the last 51200 bytes." in result
    assert "119:" in result
    assert "000:" not in result
    assert len(result.encode("utf-8")) <= _MAX_OUTPUT_BYTES + 200


@pytest.mark.asyncio
async def test_run_shell_command_truncates_many_short_lines_by_rendered_bytes(tmp_path: Path) -> None:
    """Rendered newline separators should be included in the output byte cap."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.functions["run_shell_command"]

    script = "for _ in range(10000): print('xxxxxx')"
    result = await entrypoint([sys.executable, "-c", script], tail=10000)

    assert result.startswith("[Showing the last ")
    assert " of 10000 lines; output truncated to the last 51200 bytes." in result
    assert len(result.encode("utf-8")) <= _MAX_OUTPUT_BYTES + 200


@pytest.mark.asyncio
async def test_run_shell_command_truncates_oversized_single_stdout_line(tmp_path: Path) -> None:
    """Output without newlines should still be retained and byte-capped."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.functions["run_shell_command"]

    result = await entrypoint([sys.executable, "-c", "import sys; sys.stdout.write('A' * 100000)"])

    assert result.startswith("[Output truncated to the last")
    assert "A" * 100 in result
    assert len(result.encode("utf-8")) <= _MAX_OUTPUT_BYTES + 200


@pytest.mark.asyncio
async def test_run_shell_command_truncates_oversized_single_stderr_line(tmp_path: Path) -> None:
    """Oversized stderr lines should be surfaced instead of skipped."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.functions["run_shell_command"]

    result = await entrypoint(
        [sys.executable, "-c", "import sys; sys.stderr.write('B' * 100000); sys.exit(1)"],
    )

    assert result.startswith("Exit code 1\nStderr:\n[Output truncated to the last")
    assert "B" * 100 in result
    assert len(result.encode("utf-8")) <= _MAX_OUTPUT_BYTES + 200


@pytest.mark.asyncio
async def test_run_shell_command_error_on_bad_command(tmp_path: Path) -> None:
    """Non-existent command should return an error."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.functions["run_shell_command"]

    result = await entrypoint(["__nonexistent_command_xyz__"])
    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_run_shell_command_respects_base_dir(tmp_path: Path) -> None:
    """Shell should execute in base_dir when configured."""
    runtime_paths = _make_runtime_paths(tmp_path)
    tool = get_tool_by_name(
        "shell",
        runtime_paths,
        runs_in_worker=False,
        worker_target=None,
        tool_init_overrides={"base_dir": str(tmp_path)},
        tools=builtin_tool_registry(),
    )
    entrypoint = tool.functions["run_shell_command"]

    result = await entrypoint(["pwd"])
    assert result == str(tmp_path)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def test_shell_registers_only_run_shell_command(tmp_path: Path) -> None:
    """A job is how a long command runs in the background, so the shell has no handle functions."""
    tool = _get_toolkit(tmp_path)

    assert list(tool.functions) == ["run_shell_command"]


def test_shell_disabled_registers_no_tools(tmp_path: Path) -> None:
    """Shell toolkit with enable_run_shell_command=False should have no tools."""
    runtime_paths = _make_runtime_paths(tmp_path)

    cls = shell_tools()
    toolkit = cls(enable_run_shell_command=False, runtime_paths=runtime_paths)
    assert toolkit.functions == {}


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

    env = _shell_subprocess_env(
        {},
        call_environment=ToolEnvironment(),
        base_process_env={"HOME": "/home/host-user"},
        workspace_dir=workspace,
    )

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
        call_environment=ToolEnvironment(),
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

    env = _shell_subprocess_env(
        {},
        call_environment=ToolEnvironment(),
        base_process_env=base_env,
        workspace_dir=tmp_path / "other-base-dir",
    )

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
        runs_in_worker=False,
        worker_target=None,
        tool_init_overrides={"base_dir": str(workspace)},
        tools=builtin_tool_registry(),
    )
    entrypoint = tool.functions["run_shell_command"]

    result = await entrypoint('printf %s "$MINDROOM_AGENT_WORKSPACE"')
    assert result == str(workspace.resolve())


def test_run_shell_command_description_uses_portable_workspace_paths(tmp_path: Path) -> None:
    """The shell description should give workspace guidance valid in every execution mode."""
    workspace = tmp_path / "workspace"
    runtime_paths = _make_runtime_paths(tmp_path, process_env={"HOME": "/home/host-user"})
    tool = get_tool_by_name(
        "shell",
        runtime_paths,
        runs_in_worker=False,
        worker_target=None,
        tool_init_overrides={"base_dir": str(workspace)},
        tools=builtin_tool_registry(),
    )

    description = _get_run_shell_command_function(tool).description
    assert description is not None
    assert "Always use relative paths" in description
    assert "$MINDROOM_AGENT_WORKSPACE" in description
    assert "worker-routed execution maps `~` to the workspace" in description
    assert "local execution maps it to the host home" in description
    assert description.count("Working method:") == 1
    assert "[cwd:" not in description
    assert "until <check>; do sleep 10; done" in description


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
        tools=builtin_tool_registry(),
    )

    description = _get_run_shell_command_function(tool).description
    assert description is not None
    assert "worker-routed execution maps `~` to the workspace" in description
    assert "`~` and `$HOME` point at the host home" not in description


def test_run_shell_command_description_has_no_workspace_note_without_base_dir(tmp_path: Path) -> None:
    """Without a workspace there is no cwd contract to describe, but the working method still applies."""
    tool = _get_toolkit(tmp_path)

    description = _get_run_shell_command_function(tool).description
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
    tool = get_tool_by_name(
        "shell",
        runtime_paths,
        runs_in_worker=False,
        worker_target=None,
        tools=builtin_tool_registry(),
    )
    entrypoint = tool.functions["run_shell_command"]

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
    tool = get_tool_by_name(
        "shell",
        runtime_paths,
        runs_in_worker=False,
        worker_target=None,
        tools=builtin_tool_registry(),
    )
    entrypoint = tool.functions["run_shell_command"]

    result = await entrypoint([str(fake_bash), "-lc", "printf '%s' \"$PATH\""])

    assert result == "/worker-env/bin:/usr/bin:/bin"


@pytest.mark.asyncio
async def test_run_shell_command_stops_a_command_past_its_timeout(tmp_path: Path) -> None:
    """A command past its timeout is stopped, and the call returns its output so far without a handle."""
    runtime_paths = _make_runtime_paths(tmp_path)
    tool = get_tool_by_name(
        "shell",
        runtime_paths,
        runs_in_worker=False,
        worker_target=None,
        tool_init_overrides={"base_dir": str(tmp_path)},
        tools=builtin_tool_registry(),
    )
    entrypoint = tool.functions["run_shell_command"]

    result = await entrypoint(["bash", "-c", "echo started; sleep 300"], timeout=1)

    assert result == "Stopped after 1s.\nOutput:\nstarted"


@pytest.mark.asyncio
async def test_only_a_job_command_runs_past_the_default_time_limit(tmp_path: Path) -> None:
    """Without a timeout, a call that runs as a background job has no limit and any other call has the default."""
    entrypoint = _get_run_shell_command_function(_get_toolkit(tmp_path)).entrypoint
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
    reports: list[str] = []

    with patch("mindroom.shell.execution._PROGRESS_INTERVAL_SECONDS", 0.1), tool_progress_reporter(reports.append):
        result = await entrypoint(["bash", "-c", "echo building; sleep 0.5; echo built"])

    assert result == "building\nbuilt"
    assert reports[0] == "building"


@pytest.mark.asyncio
async def test_a_progress_meter_reports_only_its_latest_state(tmp_path: Path) -> None:
    """A line a command redraws with carriage returns, such as a download meter, reports only what it shows last."""
    entrypoint = _get_run_shell_command_function(_get_toolkit(tmp_path)).entrypoint
    reports: list[str] = []
    command = "printf 'fetching\\n10%%\\r50%%\\r100%%'; timeout 0.5 tail -f /dev/null; echo"

    with patch("mindroom.shell.execution._PROGRESS_INTERVAL_SECONDS", 0.1), tool_progress_reporter(reports.append):
        await entrypoint(["bash", "-c", command])

    assert reports[0] == "fetching\n100%"


@pytest.mark.asyncio
async def test_a_running_commands_output_is_reported_whole(tmp_path: Path) -> None:
    """A private key longer than the shown output is reported from its first line, so the shown output masks it."""
    entrypoint = _get_run_shell_command_function(_get_toolkit(tmp_path)).entrypoint
    reports: list[str] = []
    key_lines = " ".join(f"'MIIE{index:02d}{'A' * 56}'" for index in range(25))
    command = (
        f"printf '%s\\n' provisioning '-----BEGIN PRIVATE KEY-----' {key_lines} '-----END PRIVATE KEY-----' waiting; "
        "timeout 0.5 tail -f /dev/null; echo done"
    )

    with patch("mindroom.shell.execution._PROGRESS_INTERVAL_SECONDS", 0.1), tool_progress_reporter(reports.append):
        await entrypoint(["bash", "-c", command])

    assert reports[0].startswith("provisioning\n-----BEGIN PRIVATE KEY-----")
    assert reports[0].endswith("waiting")
    assert "A" * 56 not in shown_progress(reports[0])


def test_the_stream_that_printed_last_ends_the_report() -> None:
    """Warnings on stderr do not hide a build's newer progress on stdout, which the live view shows as the tail."""
    reports: list[str] = []
    stdout, stderr = _OutputBuffer(), _OutputBuffer()

    with patch("mindroom.shell.execution._PROGRESS_INTERVAL_SECONDS", 0):
        progress = _OutputProgress(reports.append, stdout, stderr)
        stderr.append("".join(f"warning: unused variable {index}\n" for index in range(80)))
        stdout.append("step 1/2\n")
        progress.poll()
        stderr.append("error: link failed\n")
        progress.poll()

    assert reports[0].endswith("step 1/2")
    assert reports[0].startswith("Stderr:\nwarning: unused variable 0")
    assert reports[1].endswith("error: link failed")


def test_a_quiet_commands_unchanged_output_is_not_read_again() -> None:
    """A command that prints nothing new, such as a dev server waiting for requests, costs its progress checks nothing."""
    reports: list[str] = []
    stdout, stderr = _OutputBuffer(), _OutputBuffer()
    stdout.append("listening on :8000\n")

    with (
        patch("mindroom.shell.execution._PROGRESS_INTERVAL_SECONDS", 0),
        patch("mindroom.shell.execution._progress_lines", wraps=_progress_lines) as read,
    ):
        progress = _OutputProgress(reports.append, stdout, stderr)
        for _ in range(5):
            progress.poll()

    assert reports == ["listening on :8000"]
    assert read.call_count == 2


@pytest.mark.asyncio
async def test_a_command_never_reads_the_terminal_mindroom_runs_in(tmp_path: Path) -> None:
    """A command that reads stdin sees end of input instead of waiting on the primary's own stdin."""
    entrypoint = _get_run_shell_command_function(_get_toolkit(tmp_path)).entrypoint
    # A silent pipe stands in for the terminal a foreground `mindroom run` reads from.
    read_end, write_end = os.pipe()
    saved_stdin = os.dup(0)
    os.dup2(read_end, 0)
    try:
        result = await entrypoint("read -r line; echo read:$?", timeout=5)
    finally:
        os.dup2(saved_stdin, 0)
        for fd in (saved_stdin, read_end, write_end):
            os.close(fd)

    assert result == "read:1"


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [0, -5])
async def test_a_timeout_that_is_not_positive_is_rejected_before_the_command_runs(tmp_path: Path, seconds: int) -> None:
    """A zero or negative timeout would stop the command at once, so the call explains the argument instead."""
    entrypoint = _get_run_shell_command_function(_get_toolkit(tmp_path)).entrypoint
    marker = tmp_path / "ran"

    result = await entrypoint(["touch", str(marker)], timeout=seconds)

    assert result == "Error: timeout must be a positive number of seconds; omit it for the default limit."
    assert not marker.exists()


def _parse_background_receipt(message: str) -> tuple[int, str]:
    pid_match = re.search(r"moved to the background \(PID (\d+)\)\.", message)
    log_match = re.search(r"^Log: (\S+) \(receives ongoing", message, flags=re.MULTILINE)
    assert pid_match is not None, message
    assert log_match is not None, message
    return int(pid_match.group(1)), log_match.group(1)


async def _wait_for_log_marker(log_path: Path, marker: str, *, max_wait_seconds: float = 3.0) -> str:
    deadline = time.monotonic() + max_wait_seconds
    while time.monotonic() < deadline:
        if log_path.exists() and marker in (text := log_path.read_text(encoding="utf-8")):
            return text
        await asyncio.sleep(0.02)
    return log_path.read_text(encoding="utf-8") if log_path.exists() else ""


def _workspace_shell_entrypoint(tmp_path: Path) -> tuple[Path, ToolFunction]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    tool = get_tool_by_name(
        "shell",
        _make_runtime_paths(tmp_path),
        runs_in_worker=False,
        worker_target=None,
        tool_init_overrides={"base_dir": str(workspace)},
        tools=builtin_tool_registry(),
    )
    return workspace, _get_run_shell_command_function(tool)


@pytest.mark.asyncio
async def test_scoped_auto_background_untimed_streams_log_and_exits_cleanly(tmp_path: Path) -> None:
    """An untimed scoped command past the wait limit moves to the background with a live log and exit trailer."""
    workspace, fn = _workspace_shell_entrypoint(tmp_path)
    cmd = "echo before-out; echo before-err >&2; sleep 0.5; echo after-out; echo after-err >&2"

    with patch("mindroom.shell.execution._DEFAULT_RUN_TIMEOUT_SECONDS", 0.2):
        async with shell_background_scope() as scope:
            receipt = await fn.entrypoint(["bash", "-c", cmd])
            assert isinstance(receipt, str)
            assert scope.had_backgrounded is True
            pid, rel_log = _parse_background_receipt(receipt)
            assert receipt.startswith(f"Still running after 0.2s; moved to the background (PID {pid}).")
            assert rel_log.startswith(".mindroom/shell/")
            assert (
                f"Wait: end=$((SECONDS+110)); while ((SECONDS<end)) && kill -0 {pid} 2>/dev/null; do sleep 2; done"
                in receipt
            )
            assert f"Stop: kill -- -{pid}" in receipt
            assert "It is stopped automatically when this turn ends." in receipt

            log_path = workspace / rel_log
            log_text = await _wait_for_log_marker(log_path, "[MindRoom: exited with code 0]")
            assert "before-out" in log_text
            assert "before-err" in log_text
            assert "after-out" in log_text
            assert "after-err" in log_text
            assert "[MindRoom: exited with code 0]" in log_text

    # Verify receipt preview formatting deterministically without relying on subprocess startup timing.
    stdout_buf = _OutputBuffer()
    stdout_buf.append("before-out\n")
    stderr_buf = _OutputBuffer()
    stderr_buf.append("before-err\n")
    preview = _format_auto_background_message(
        pid=4242,
        timeout=0.2,
        log_path=workspace / ".mindroom" / "shell" / "4242-deadbeef.log",
        display_root=str(workspace),
        max_runtime_seconds=None,
        no_output_path=None,
        stdout_buf=stdout_buf,
        stderr_buf=stderr_buf,
        tail=10,
    )
    assert "Output:\nbefore-out" in preview
    assert "Stderr:\nbefore-err" in preview


@pytest.mark.asyncio
async def test_scoped_short_timeout_kills_inline_without_backgrounding(tmp_path: Path) -> None:
    """A scoped command with timeout <= wait limit is killed inline rather than backgrounded."""
    workspace, fn = _workspace_shell_entrypoint(tmp_path)

    with patch("mindroom.shell.execution._DEFAULT_RUN_TIMEOUT_SECONDS", 2):
        async with shell_background_scope() as scope:
            result = await fn.entrypoint(["bash", "-c", "echo start; sleep 10"], timeout=1)
            assert result == "Stopped after 1s.\nOutput:\nstart"
            assert scope.had_backgrounded is False
            assert not scope.records
            assert not (workspace / ".mindroom" / "shell").exists()


@pytest.mark.asyncio
async def test_scoped_long_timeout_backgrounds_then_kills_at_deadline(tmp_path: Path) -> None:
    """A scoped command with timeout > wait backgrounds at wait, then stops at its total timeout."""
    workspace, fn = _workspace_shell_entrypoint(tmp_path)

    with patch("mindroom.shell.execution._DEFAULT_RUN_TIMEOUT_SECONDS", 0.2):
        async with shell_background_scope():
            receipt = await fn.entrypoint(["bash", "-c", "sleep 10"], timeout=1)
            assert isinstance(receipt, str)
            pid, rel_log = _parse_background_receipt(receipt)
            assert "It will be stopped after 1s total, or automatically when this turn ends." in receipt

            log_text = await _wait_for_log_marker(workspace / rel_log, "[MindRoom: stopped after 1s timeout")
            assert "[MindRoom: stopped after 1s timeout (exit code " in log_text
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)


@pytest.mark.asyncio
async def test_scope_exit_stops_running_process_group_and_writes_trailer(tmp_path: Path) -> None:
    """Closing a shell background scope stops its running process group and writes the turn-ended trailer."""
    workspace, fn = _workspace_shell_entrypoint(tmp_path)

    with patch("mindroom.shell.execution._DEFAULT_RUN_TIMEOUT_SECONDS", 0.2):
        async with shell_background_scope():
            receipt = await fn.entrypoint(["bash", "-c", "sleep 10"])
            assert isinstance(receipt, str)
            pid, rel_log = _parse_background_receipt(receipt)
            os.kill(pid, 0)

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    log_text = (workspace / rel_log).read_text(encoding="utf-8")
    assert "[MindRoom: stopped when turn ended]" in log_text


@pytest.mark.asyncio
async def test_auto_background_with_output_path_returns_handled_and_leaves_target_untouched(tmp_path: Path) -> None:
    """Auto-backgrounding closes output capture without writing or overwriting mindroom_output_path."""
    workspace, fn = _workspace_shell_entrypoint(tmp_path)
    target = workspace / "existing.txt"
    target.write_text("keep-me", encoding="utf-8")
    policy = ToolOutputFilePolicy(workspace_root=workspace, max_bytes=1024 * 1024)
    request = prepare_tool_output_file(policy, tool_name="run_shell_command", output_path="existing.txt")
    assert not isinstance(request, dict)

    with patch("mindroom.shell.execution._DEFAULT_RUN_TIMEOUT_SECONDS", 0.2):
        async with shell_background_scope():
            token = _active_output_request.set(request)
            try:
                direct = await fn.entrypoint(["bash", "-c", "echo hi; sleep 5"])
            finally:
                _active_output_request.reset(token)
            assert isinstance(direct, ToolOutputFileHandled)
            assert isinstance(direct.result, str)
            assert "No output file was saved to existing.txt because the command is still running." in direct.result
            assert target.read_text(encoding="utf-8") == "keep-me"

            wrapped = with_output_files(fn, policy)
            wrapped_result = await wrapped.entrypoint(["bash", "-c", "sleep 5"], mindroom_output_path="existing.txt")
            assert isinstance(wrapped_result, str)
            assert "No output file was saved to existing.txt because the command is still running." in wrapped_result
            assert target.read_text(encoding="utf-8") == "keep-me"


@pytest.mark.asyncio
async def test_background_log_size_cap_writes_truncation_notice_and_exit_trailer(tmp_path: Path) -> None:
    """A background log caps body bytes with one notice while still appending the exit trailer."""
    log_dir = tmp_path / "logs"
    registry: dict[str, ProcessRecord] = {}
    cmd = 'python3 -c \'import time; print("A" * 80, flush=True); time.sleep(0.35); print("B" * 200, flush=True)\''
    try:
        result = await run_command(
            registry,
            namespace="cap-test",
            argv=["bash", "-c", cmd],
            env=dict(os.environ),
            cwd=str(tmp_path),
            tail=10,
            timeout=0.15,
            on_timeout="background",
            background_log=BackgroundLogPolicy(log_dir=str(log_dir), max_bytes=120),
        )
        assert result.handle is not None
        log_path = Path(registry[result.handle]._log_path or "")
        log_text = await _wait_for_log_marker(log_path, "[MindRoom: exited with code 0]")
        assert "[Log truncated at 120 bytes limit; command output continues without being logged.]" in log_text
        assert log_text.count("[Log truncated") == 1
        assert "[MindRoom: exited with code 0]" in log_text
    finally:
        kill_namespace_records(registry, "cap-test")


def test_background_log_exact_limit_does_not_truncate_until_overflow(tmp_path: Path) -> None:
    """Writing exactly max_bytes does not emit a truncation notice until extra bytes actually overflow."""
    policy = BackgroundLogPolicy(log_dir=str(tmp_path / "exact-limit"), max_bytes=10)
    exact = open_background_log(policy, pid=100, handle="11111111", active_paths=set())
    exact.write("0123456789")
    exact.finish("\n[MindRoom: exited with code 0]\n")
    exact_text = exact.path.read_text(encoding="utf-8")
    assert exact_text == "0123456789\n[MindRoom: exited with code 0]\n"

    overflow = open_background_log(policy, pid=101, handle="22222222", active_paths=set())
    overflow.write("0123456789")
    overflow.write("X")
    overflow.finish("\n[MindRoom: exited with code 0]\n")
    overflow_text = overflow.path.read_text(encoding="utf-8")
    assert overflow_text.startswith("0123456789\n[Log truncated at 10 bytes limit;")
    assert "X" not in overflow_text
    assert overflow_text.endswith("\n[MindRoom: exited with code 0]\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("symlink_target", [".mindroom", ".mindroom/shell"])
async def test_background_log_rejects_symlink_directories(tmp_path: Path, symlink_target: str) -> None:
    """Auto-backgrounding refuses symlinked .mindroom or .mindroom/shell and kills the command."""
    workspace, fn = _workspace_shell_entrypoint(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / symlink_target
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)

    with patch("mindroom.shell.execution._DEFAULT_RUN_TIMEOUT_SECONDS", 0.2):
        async with shell_background_scope() as scope:
            result = await fn.entrypoint(["bash", "-c", "sleep 10"])
            assert isinstance(result, str)
            assert result.startswith("Error: Failed to open background shell log:")
            assert not scope.records
            assert list(outside.iterdir()) == []


def test_background_rejection_enforces_per_namespace_and_total_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    """_background_rejection enforces closed scope, per-namespace cap, total cap, and duplicate handles."""
    record = ProcessRecord(
        namespace="ns-a",
        handle="shell:11111111",
        pid=1234,
        args=["sleep", "10"],
        process=SimpleNamespace(pid=1234, returncode=None),  # type: ignore[arg-type]
    )
    registry = {record.handle: record}
    closed_scope = ShellBackgroundScope(records=registry, closed=True)
    assert _background_rejection(registry, namespace="ns-a", handle="shell:22222222", active_scope=closed_scope) == (
        "Error: Shell turn already ended."
    )
    monkeypatch.setattr("mindroom.shell.execution.MAX_BACKGROUNDED", 1)
    assert _background_rejection(registry, namespace="ns-a", handle="shell:22222222", active_scope=None) == (
        "Error: Too many backgrounded processes (1/1). Kill or wait for existing ones before running more."
    )
    assert _background_rejection(registry, namespace="ns-b", handle="shell:22222222", active_scope=None) is None
    monkeypatch.setattr("mindroom.shell.execution._MAX_TOTAL_BACKGROUNDED", 1)
    assert _background_rejection(registry, namespace="ns-b", handle="shell:22222222", active_scope=None) == (
        "Error: Too many backgrounded processes (1/1). Kill or wait for existing ones before running more."
    )
    monkeypatch.setattr("mindroom.shell.execution.MAX_BACKGROUNDED", 4)
    monkeypatch.setattr("mindroom.shell.execution._MAX_TOTAL_BACKGROUNDED", 4)
    assert _background_rejection(registry, namespace="ns-a", handle=record.handle, active_scope=None) == (
        f"Error: Shell handle '{record.handle}' is already registered."
    )


def test_shell_request_budget_caps_auto_background_wait() -> None:
    """auto_background=True caps foreground request budget at the default wait plus grace."""
    assert shell_request_budget_seconds("shell", "run_shell_command", {"timeout": 600}) == 630.0
    assert shell_request_budget_seconds("shell", "run_shell_command", {"timeout": 600}, auto_background=True) == 150.0
    assert shell_request_budget_seconds("shell", "run_shell_command", {"timeout": 30}, auto_background=True) == 60.0
    assert shell_request_budget_seconds("shell", "run_shell_command", {}, auto_background=True) == 150.0


def test_background_log_dir_ownership_checks_leaf_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A foreign-owned .mindroom parent is allowed when the leaf log directory is owned by the current user."""
    from mindroom.shell import background_scope as bg_module  # noqa: PLC0415

    mounted_ws = tmp_path / "mounted"
    parent_mindroom = mounted_ws / ".mindroom"
    leaf_shell = parent_mindroom / "shell"
    leaf_shell.mkdir(parents=True)
    parent_ino = parent_mindroom.stat().st_ino
    real_fstat = os.fstat

    def fake_fstat(fd: int) -> object:
        st = real_fstat(fd)
        return SimpleNamespace(st_uid=os.geteuid() + 1) if st.st_ino == parent_ino else st

    monkeypatch.setattr(os, "fstat", fake_fstat)
    with bg_module._open_log_dir(leaf_shell):
        pass
    monkeypatch.undo()

    owned_dir = tmp_path / "foreign"
    owned_dir.mkdir()
    monkeypatch.setattr(os, "geteuid", lambda: owned_dir.stat().st_uid + 1)
    with pytest.raises(PermissionError, match="not owned by the current user"), bg_module._open_log_dir(owned_dir):
        pass


@pytest.mark.asyncio
async def test_background_log_sweep_filters_generated_names_and_kill_all_trailer(tmp_path: Path) -> None:
    """Sweep only touches generated <pid>-<hex>.log names, protects recent logs, and kill_all writes a trailer."""
    from mindroom.shell import background_scope as bg_module  # noqa: PLC0415
    from mindroom.shell.execution import kill_all_records  # noqa: PLC0415

    log_dir = tmp_path / "sweep"
    log_dir.mkdir()
    now = time.time()
    expired = log_dir / "1001-deadbeef.log"
    expired.write_text("old", encoding="utf-8")
    os.utime(expired, (now - 25 * 3600, now - 25 * 3600))
    old_evict = log_dir / "1002-cafebabe.log"
    old_evict.write_text("evict", encoding="utf-8")
    os.utime(old_evict, (now - 600, now - 600))
    unrelated = log_dir / "build.log"
    unrelated.write_text("keep", encoding="utf-8")
    os.utime(unrelated, (now - 25 * 3600, now - 25 * 3600))
    recent_files = []
    for idx in range(bg_module._LOG_MAX_FILES + 2):
        recent = log_dir / f"{2000 + idx}-{idx:08x}.log"
        recent.write_text("live", encoding="utf-8")
        recent_files.append(recent)
    with bg_module._open_log_dir(log_dir) as dir_fd:
        bg_module._sweep_old_log_files(dir_fd, active_names=set())
    assert not expired.exists()
    assert not old_evict.exists()
    assert unrelated.exists()
    assert all(path.exists() for path in recent_files)

    # kill_all_records writes a stop trailer to active auto-backgrounded logs.
    registry: dict[str, ProcessRecord] = {}
    policy = BackgroundLogPolicy(log_dir=str(tmp_path / "kill-all-logs"))
    result = await run_command(
        registry,
        namespace="shutdown-test",
        argv=["bash", "-c", "echo hi; sleep 30"],
        env=dict(os.environ),
        cwd=str(tmp_path),
        tail=10,
        timeout=0.15,
        on_timeout="background",
        background_log=policy,
    )
    assert result.handle is not None
    log_path = Path(registry[result.handle]._log_path or "")
    kill_all_records(registry)
    assert "[MindRoom: stopped]" in log_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_auto_backgrounded_natural_exit_preserves_detached_nohup_child(tmp_path: Path) -> None:
    """An auto-backgrounded command that exits naturally leaves detached nohup children alive."""
    workspace, fn = _workspace_shell_entrypoint(tmp_path)
    detached_pid_file = workspace / "detached.pid"
    cmd = f"sleep 0.35; nohup sleep 30 >/dev/null 2>&1 & echo $! > {detached_pid_file}; exit 0"
    detached_pid: int | None = None
    try:
        with patch("mindroom.shell.execution._DEFAULT_RUN_TIMEOUT_SECONDS", 0.15):
            async with shell_background_scope() as scope:
                result = await fn.entrypoint(["bash", "-c", cmd])
                assert isinstance(result, str)
                assert "Still running after 0.15s" in result
                assert len(scope.records) == 1
                record = next(iter(scope.records.values()))
                assert record._monitor_task is not None
                await asyncio.wait_for(record._monitor_task, timeout=5.0)
                assert record.finished
                assert record.return_code == 0
                detached_pid = int(detached_pid_file.read_text(encoding="utf-8").strip())
                os.kill(detached_pid, 0)
                log_text = Path(record._log_path or "").read_text(encoding="utf-8")
                assert "[MindRoom: exited with code 0]" in log_text
        # Still alive after the scope closes cleanly because the parent command already exited naturally.
        os.kill(detached_pid, 0)
    finally:
        if detached_pid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(detached_pid, signal.SIGKILL)


@pytest.mark.asyncio
async def test_sigterm_to_backgrounded_leader_still_kills_sigterm_ignoring_child(tmp_path: Path) -> None:
    """Stopping a backgrounded leader via kill -- -PID still SIGKILLs children that ignore SIGTERM."""
    workspace, fn = _workspace_shell_entrypoint(tmp_path)
    child_pid_file = workspace / "child.pid"
    child_script = (
        "import os, pathlib, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "tmp = pathlib.Path(sys.argv[1] + '.tmp'); "
        "tmp.write_text(str(os.getpid()), encoding='utf-8'); "
        "os.replace(tmp, sys.argv[1]); "
        "time.sleep(30)"
    )
    parent_script = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); "
        "time.sleep(30)"
    )
    child_pid: int | None = None
    try:
        with patch("mindroom.shell.execution._DEFAULT_RUN_TIMEOUT_SECONDS", 0.2):
            async with shell_background_scope() as scope:
                result = await fn.entrypoint([sys.executable, "-c", parent_script, child_script, str(child_pid_file)])
                assert isinstance(result, str)
                assert "Still running after 0.2s" in result
                record = next(iter(scope.records.values()))
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    if child_pid_file.exists():
                        break
                    await asyncio.sleep(0.02)
                assert child_pid_file.exists()
                child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
                os.kill(child_pid, 0)
                # Simulate model running `kill -- -PID` (SIGTERM to process group).
                os.killpg(record.pid, signal.SIGTERM)
                assert record._monitor_task is not None
                await asyncio.wait_for(record._monitor_task, timeout=5.0)
                assert record.return_code == -signal.SIGTERM
                if sys.platform == "linux":
                    await assert_linux_pid_not_running(child_pid)
                else:
                    kill_deadline = time.monotonic() + 2.0
                    while time.monotonic() < kill_deadline:
                        try:
                            os.kill(child_pid, 0)
                        except ProcessLookupError:
                            break
                        await asyncio.sleep(0.02)
                    else:
                        pytest.fail(f"SIGTERM-ignoring child {child_pid} stayed alive after leader SIGTERM")
    finally:
        if child_pid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(child_pid, signal.SIGKILL)
