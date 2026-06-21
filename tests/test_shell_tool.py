"""Tests for the shell tool structured execution helper."""

from __future__ import annotations

import contextlib
import os
import signal
import sys
from typing import TYPE_CHECKING

import pytest

from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.tool_system.metadata import TOOL_METADATA
from mindroom.tools.shell import _process_registry, shell_tools

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from agno.tools.toolkit import Toolkit


@pytest.fixture(autouse=True)
def clear_process_registry() -> Iterator[None]:
    """Ensure subprocess registry state does not leak between tests."""
    _process_registry.clear()
    yield
    for record in list(_process_registry.values()):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(record.pid, signal.SIGKILL)
    _process_registry.clear()


def _make_runtime_paths(tmp_path: Path) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )


def _get_toolkit(tmp_path: Path) -> Toolkit:
    return shell_tools()(runtime_paths=_make_runtime_paths(tmp_path))


@pytest.mark.asyncio
async def test_run_shell_command_structured_returns_json_safe_mapping(tmp_path: Path) -> None:
    """Structured shell execution should expose machine-readable process fields."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command_structured"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('out'); sys.stderr.write('err'); sys.exit(7)",
        ],
    )

    assert type(result) is dict
    assert result == {
        "ok": False,
        "exit_code": 7,
        "stdout": "out",
        "stderr": "err",
        "raw_output": "out\nerr",
        "timed_out": False,
        "error": None,
    }


@pytest.mark.asyncio
async def test_run_shell_command_structured_enforces_byte_cap_on_returned_output(tmp_path: Path) -> None:
    """The structured helper should cap stdout, stderr, and raw output independently."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command_structured"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('abcdef'); sys.stderr.write('uvwxyz')",
        ],
        max_output_bytes=3,
    )

    assert result["stdout"] == "def"
    assert result["stderr"] == "xyz"
    assert result["raw_output"].encode("utf-8") == b"xyz"
    assert len(result["stdout"].encode("utf-8")) <= 3
    assert len(result["stderr"].encode("utf-8")) <= 3
    assert len(result["raw_output"].encode("utf-8")) <= 3


@pytest.mark.asyncio
async def test_run_shell_command_structured_rejects_output_cap_above_hard_limit(tmp_path: Path) -> None:
    """Callers should not be able to allocate unbounded output buffers."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command_structured"].entrypoint
    assert entrypoint is not None

    result = await entrypoint("true", max_output_bytes=64 * 1024 + 1)

    assert result == {
        "ok": False,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "raw_output": "",
        "timed_out": False,
        "error": "max_output_bytes must be between 1 and 65536.",
    }


@pytest.mark.asyncio
async def test_run_shell_command_structured_zero_tail_returns_empty_output(tmp_path: Path) -> None:
    """A zero-line tail should return no output, not bypass output limiting."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command_structured"].entrypoint
    assert entrypoint is not None

    result = await entrypoint(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('out'); sys.stderr.write('err')",
        ],
        tail=0,
    )

    assert result["stdout"] == ""
    assert result["stderr"] == ""
    assert result["raw_output"] == ""


@pytest.mark.asyncio
async def test_run_shell_command_structured_timeout_terminates_without_background_handle(tmp_path: Path) -> None:
    """Structured timeouts should stop the process instead of returning a background handle."""
    tool = _get_toolkit(tmp_path)
    entrypoint = tool.async_functions["run_shell_command_structured"].entrypoint
    assert entrypoint is not None
    pid_file = tmp_path / "sleep.pid"

    result = await entrypoint(
        [
            sys.executable,
            "-c",
            (
                "import os, pathlib, sys, time; "
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
                "time.sleep(30)"
            ),
            str(pid_file),
        ],
        timeout=0,
    )

    assert result["ok"] is False
    assert result["exit_code"] is None
    assert result["timed_out"] is True
    assert result["error"] == "Command timed out after 0s."
    assert "Handle:" not in result["raw_output"]
    assert _process_registry == {}


def test_shell_metadata_and_toolkit_include_structured_function(tmp_path: Path) -> None:
    """The structured helper should be registered alongside run_shell_command."""
    assert "run_shell_command_structured" in TOOL_METADATA["shell"].function_names

    tool = _get_toolkit(tmp_path)

    assert "run_shell_command" in tool.async_functions
    assert "run_shell_command_structured" in tool.async_functions
