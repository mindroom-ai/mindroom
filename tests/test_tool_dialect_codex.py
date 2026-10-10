"""Tests for the Codex CLI tool dialect."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from agno.tools.function import Function

from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.shell_execution import MAX_OUTPUT_LINES
from mindroom.tool_dialects.claude import CLAUDE_DIALECT
from mindroom.tool_dialects.codex import CODEX_DIALECT
from mindroom.tool_dialects.translation import canonical_tool_calls, resolve_tool_dialect, wire_tools
from mindroom.tool_dialects.types import DialectArgumentError, ToolDialect, WireFunction
from mindroom.tool_system.metadata import get_tool_by_name

if TYPE_CHECKING:
    from pathlib import Path


def _wire(name: str) -> WireFunction:
    return next(function for function in CODEX_DIALECT.functions if function.wire_name == name)


def _function(name: str, toolkit: str) -> Function:
    function = Function(name=name, description=f"canonical {name}", parameters={"type": "object", "properties": {}})
    function.owning_toolkit = toolkit
    return function


def test_exec_command_maps_cmd_workdir_yield() -> None:
    """exec_command's cmd, workdir, and yield time become the canonical command, workdir, and timeout."""
    to_canonical = _wire("exec_command").to_canonical

    assert to_canonical({"cmd": "pytest", "workdir": "pkg", "yield_time_ms": 1500, "max_output_tokens": 999}) == {
        "args": "pytest",
        "tail": MAX_OUTPUT_LINES,
        "workdir": "pkg",
        "timeout": 2,
    }
    assert to_canonical({"cmd": "ls", "yield_time_ms": 10}) == {"args": "ls", "tail": MAX_OUTPUT_LINES, "timeout": 1}
    with pytest.raises(DialectArgumentError, match="exec_command requires cmd"):
        to_canonical({"command": "ls"})


def test_history_argv_with_non_strings_still_renders() -> None:
    """A recorded call whose argv holds non-strings, which the shell tool rejected, still renders as a command."""
    assert _wire("exec_command").to_wire({"args": ["head", "-n", 5, "f"]}) == {"cmd": "head -n 5 f"}


def test_session_id_is_the_handle() -> None:
    """Session IDs are MindRoom's command handles, in both directions."""
    write_stdin = _wire("write_stdin")

    assert write_stdin.to_canonical({"session_id": "shell:0123abcd", "yield_time_ms": 30000}) == {
        "handle": "shell:0123abcd",
        "wait": 30,
    }
    assert write_stdin.to_wire({"handle": "shell:0123abcd", "wait": 30}) == {
        "session_id": "shell:0123abcd",
        "yield_time_ms": 30000,
    }


def test_empty_poll_waits_like_codex() -> None:
    """An empty poll waits at least 5 seconds, as Codex clamps it, and at most MindRoom's 60."""
    to_canonical = _wire("write_stdin").to_canonical

    assert to_canonical({"session_id": "shell:1"}) == {"handle": "shell:1", "wait": 5}
    assert to_canonical({"session_id": "shell:1", "chars": "", "yield_time_ms": 900000}) == {
        "handle": "shell:1",
        "wait": 60,
    }


def test_write_stdin_with_chars_is_a_tool_error() -> None:
    """Writing input is unsupported and says so instead of silently polling."""
    with pytest.raises(DialectArgumentError, match="write_stdin cannot send input; pass empty chars to poll"):
        _wire("write_stdin").to_canonical({"session_id": "shell:1", "chars": "y\n"})


@pytest.mark.parametrize(
    ("wire_name", "canonical"),
    [
        ("exec_command", {"args": "ls -la", "tail": MAX_OUTPUT_LINES, "workdir": "src", "timeout": 3}),
        ("write_stdin", {"handle": "shell:0123abcd", "wait": 7}),
        ("apply_patch", {"input": "*** Begin Patch\n*** Delete File: a\n*** End Patch"}),
    ],
)
def test_round_trip_for_every_function(wire_name: str, canonical: dict[str, object]) -> None:
    """Canonical history renders into wire arguments that translate back to the same call."""
    wire = _wire(wire_name)

    assert wire.to_canonical(wire.to_wire(canonical)) == canonical


def _names(dialect_tools: list[Function | dict[str, object]]) -> list[str]:
    return sorted(
        tool.name if isinstance(tool, Function) else str(tool.get("name") or tool["function"]["name"])
        for tool in dialect_tools
    )


def test_edit_and_write_hidden_apply_patch_shown() -> None:
    """Codex models edit with apply_patch; Claude and other models edit with edit_file and write_file."""
    coding = [_function(name, "coding") for name in ("edit_file", "write_file", "apply_patch", "read_file")]

    assert _names(wire_tools(CODEX_DIALECT, coding, custom_tools=False)) == ["apply_patch", "read_file"]
    assert _names(wire_tools(CLAUDE_DIALECT, coding, custom_tools=False)) == ["Edit", "Read", "Write"]
    assert _names(wire_tools(resolve_tool_dialect(None), coding, custom_tools=False)) == [
        "edit_file",
        "read_file",
        "write_file",
    ]


def test_file_edits_stay_when_filters_removed_their_replacement() -> None:
    """A dialect hides a file-edit function only beside its replacement, so a filtered toolkit can still edit."""
    edit_and_write = [_function(name, "coding") for name in ("edit_file", "write_file", "read_file")]
    patch_only = [_function(name, "coding") for name in ("apply_patch", "read_file")]

    assert _names(wire_tools(CODEX_DIALECT, edit_and_write, custom_tools=False)) == [
        "edit_file",
        "read_file",
        "write_file",
    ]
    assert _names(wire_tools(CLAUDE_DIALECT, patch_only, custom_tools=False)) == ["Read", "apply_patch"]
    assert _names(wire_tools(resolve_tool_dialect(None), patch_only, custom_tools=False)) == [
        "apply_patch",
        "read_file",
    ]


def test_apply_patch_custom_format_only_with_custom_tools() -> None:
    """The freeform grammar goes only to models that accept custom tools; others get a JSON input argument."""
    [custom] = wire_tools(CODEX_DIALECT, [_function("apply_patch", "coding")], custom_tools=True)
    [function] = wire_tools(CODEX_DIALECT, [_function("apply_patch", "coding")], custom_tools=False)

    assert isinstance(custom, dict)
    assert custom["type"] == "custom"
    assert custom["format"]["syntax"] == "lark"
    assert custom["format"]["definition"].startswith("start: begin_patch hunk+ end_patch")
    assert isinstance(function, dict)
    assert function["function"]["parameters"]["required"] == ["input"]


@pytest.mark.parametrize(
    ("provider", "model_id"),
    [("openai", "gpt-6-astra"), ("codex", "gpt-6.1-sol"), ("openrouter", "openai/gpt-6-luna")],
)
def test_resolve_tool_dialect_returns_codex_for_openai_and_codex(provider: str, model_id: str) -> None:
    """OpenAI GPT and Codex models get the Codex dialect."""
    assert resolve_tool_dialect(ModelConfig(provider=provider, id=model_id)) is CODEX_DIALECT


def test_kill_shell_command_takes_the_session_id() -> None:
    """Codex models stop a session by the handle they were shown."""
    kill = _wire("kill_shell_command")

    assert kill.to_canonical({"session_id": "shell:0123abcd"}) == {"handle": "shell:0123abcd", "force": False}
    assert kill.to_canonical({"session_id": "shell:1", "force": True}) == {"handle": "shell:1", "force": True}
    assert kill.to_wire({"handle": "shell:0123abcd", "force": True}) == {"session_id": "shell:0123abcd", "force": True}


def test_exec_command_yields_like_codex() -> None:
    """exec_command waits 10 seconds by default and at most 30, as Codex does."""
    to_canonical = _wire("exec_command").to_canonical

    assert to_canonical({"cmd": "ls"}) == {"args": "ls", "tail": MAX_OUTPUT_LINES, "timeout": 10}
    assert to_canonical({"cmd": "ls", "yield_time_ms": 600000}) == {
        "args": "ls",
        "tail": MAX_OUTPUT_LINES,
        "timeout": 30,
    }


def test_kill_shell_command_call_dispatches_with_the_handle() -> None:
    """A Codex model's kill_shell_command(session_id) reaches the canonical function with its handle."""
    kill = _function("kill_shell_command", "shell")
    call = {
        "id": "c1",
        "type": "function",
        "function": {"name": "kill_shell_command", "arguments": json.dumps({"session_id": "shell:0123abcd"})},
    }

    [translated], errors = canonical_tool_calls(CODEX_DIALECT, [call], {"kill_shell_command": kill})

    assert errors == []
    assert json.loads(translated["function"]["arguments"]) == {"handle": "shell:0123abcd", "force": False}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dialect", "wire_name", "arguments"),
    [(CLAUDE_DIALECT, "Bash", {"command": "seq 1 250"}), (CODEX_DIALECT, "exec_command", {"cmd": "seq 1 250"})],
    ids=["claude", "codex"],
)
async def test_harness_shell_calls_return_more_than_the_default_tail(
    tmp_path: Path,
    dialect: ToolDialect,
    wire_name: str,
    arguments: dict[str, object],
) -> None:
    """Harness shell calls return output whole up to the byte cap, not just the canonical last 100 lines."""
    (tmp_path / ".env").write_text("", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    shell = get_tool_by_name("shell", runtime_paths, disable_sandbox_proxy=True, worker_target=None)
    run = shell.async_functions["run_shell_command"].entrypoint
    assert run is not None
    wire = next(function for function in dialect.functions if function.wire_name == wire_name)

    output = await run(**wire.to_canonical(arguments))

    lines = output.splitlines()
    assert lines[lines.index("1") :] == [str(number) for number in range(1, 251)]
