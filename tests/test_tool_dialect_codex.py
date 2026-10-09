"""Tests for the Codex CLI tool dialect."""

from __future__ import annotations

import json

import pytest
from agno.tools.function import Function

from mindroom.config.models import ModelConfig
from mindroom.shell_execution import _format_background_handle_message, _format_finished_status, _format_running_status
from mindroom.tool_dialect_claude import CLAUDE_DIALECT
from mindroom.tool_dialect_codex import CODEX_DIALECT
from mindroom.tool_dialect_types import MINDROOM_WIRE_KEY, DialectArgumentError, WireFunction
from mindroom.tool_dialects import canonical_tool_calls, resolve_tool_dialect, wire_tools
from mindroom.tool_system.tool_access import ToolKey


def _wire(name: str) -> WireFunction:
    return next(function for function in CODEX_DIALECT.functions if function.wire_name == name)


def _render(name: str, text: str) -> str:
    render = _wire(name).render_result
    assert render is not None
    return render(text)


def _function(name: str, toolkit: str) -> Function:
    function = Function(name=name, description=f"canonical {name}", parameters={"type": "object", "properties": {}})
    function.owning_toolkit = toolkit
    return function


def test_exec_command_maps_cmd_workdir_yield() -> None:
    """exec_command's cmd, workdir, and yield time become the canonical command, workdir, and timeout."""
    to_canonical = _wire("exec_command").to_canonical

    assert to_canonical({"cmd": "pytest", "workdir": "pkg", "yield_time_ms": 1500, "max_output_tokens": 999}) == {
        "args": "pytest",
        "workdir": "pkg",
        "timeout": 2,
    }
    assert to_canonical({"cmd": "ls", "yield_time_ms": 10}) == {"args": "ls", "timeout": 1}
    with pytest.raises(DialectArgumentError, match="exec_command requires cmd"):
        to_canonical({"command": "ls"})


def test_session_id_round_trips_handles() -> None:
    """Session IDs are the numeric value of the handle's hex digits, in both directions."""
    write_stdin = _wire("write_stdin")

    assert write_stdin.to_canonical({"session_id": 0x0123ABCD, "yield_time_ms": 30000}) == {
        "handle": "shell:0123abcd",
        "wait": 30,
    }
    assert write_stdin.to_wire({"handle": "shell:0123abcd", "wait": 30}) == {
        "session_id": 0x0123ABCD,
        "yield_time_ms": 30000,
    }
    long_handle = "shell:" + "f" * 32
    assert write_stdin.to_canonical(write_stdin.to_wire({"handle": long_handle, "wait": 5})) == {
        "handle": long_handle,
        "wait": 5,
    }


def test_empty_poll_waits_like_codex() -> None:
    """An empty poll waits at least 5 seconds, as Codex clamps it, and at most MindRoom's 60."""
    to_canonical = _wire("write_stdin").to_canonical

    assert to_canonical({"session_id": 1}) == {"handle": "shell:00000001", "wait": 5}
    assert to_canonical({"session_id": 1, "chars": "", "yield_time_ms": 900000}) == {
        "handle": "shell:00000001",
        "wait": 60,
    }


def test_write_stdin_with_chars_is_a_tool_error() -> None:
    """Writing input is unsupported and says so instead of silently polling."""
    with pytest.raises(DialectArgumentError, match="write_stdin cannot send input; pass empty chars to poll"):
        _wire("write_stdin").to_canonical({"session_id": 1, "chars": "y\n"})


def test_stale_session_id_returns_unknown_handle_error() -> None:
    """A poll for a session MindRoom no longer knows names the session ID the model used."""
    assert _render("write_stdin", "Error: Unknown handle 'shell:0123abcd'") == f"Error: Unknown session ID {0x0123ABCD}"


def test_status_renderings() -> None:
    """Background messages and poll results use Codex's process wording."""
    background = "[cwd: /w]\n" + _format_background_handle_message(10, 77, "shell:0123abcd")
    finished = _format_finished_status(return_code=2, elapsed=1.5, stderr="boom", output="out")
    running = _format_running_status(pid=77, elapsed=3.0, buffered_lines=1, partial="partial")

    assert _render("exec_command", background) == (
        f"[cwd: /w]\nWall time: 10 seconds\nProcess running with session ID {0x0123ABCD} (PID 77); poll it with "
        f"write_stdin or stop it with kill_shell_command(session_id={0x0123ABCD})\nOutput:\n"
    )
    assert (
        _render("write_stdin", finished)
        == "Wall time: 1.5 seconds\nProcess exited with code 2\nOutput:\nout\nStderr:\nboom"
    )
    assert _render("write_stdin", running) == "Wall time: 3 seconds\nProcess running (PID 77)\nOutput:\npartial"
    printed = "grep hit: check_shell_command('shell:0123abcd')"
    assert _render("exec_command", printed) == printed
    status_output = _format_finished_status(return_code=0, elapsed=1.0, stderr="", output=printed)
    assert (
        _render("write_stdin", status_output) == f"Wall time: 1 seconds\nProcess exited with code 0\nOutput:\n{printed}"
    )
    assert _render("exec_command", "[cwd: /w]\nplain output") == "[cwd: /w]\nplain output"


@pytest.mark.parametrize(
    ("wire_name", "canonical"),
    [
        ("exec_command", {"args": "ls -la", "workdir": "src", "timeout": 3}),
        ("write_stdin", {"handle": "shell:0123abcd", "wait": 7}),
        ("apply_patch", {"input": "*** Begin Patch\n*** Delete File: a\n*** End Patch"}),
    ],
)
def test_round_trip_for_every_function(wire_name: str, canonical: dict[str, object]) -> None:
    """Canonical history renders into wire arguments that translate back to the same call."""
    wire = _wire(wire_name)

    assert wire.to_canonical(wire.to_wire(canonical)) == canonical


def test_edit_and_write_hidden_apply_patch_shown() -> None:
    """Codex models edit with apply_patch; Claude and other models never see it."""
    coding = [_function(name, "coding") for name in ("edit_file", "write_file", "apply_patch", "read_file")]

    def names(dialect_tools: list[Function | dict[str, object]]) -> list[str]:
        return sorted(
            tool.name if isinstance(tool, Function) else str(tool.get("name") or tool["function"]["name"])
            for tool in dialect_tools
        )

    assert names(wire_tools(CODEX_DIALECT, coding, custom_tools=False)) == ["apply_patch", "read_file"]
    assert names(wire_tools(CLAUDE_DIALECT, coding, custom_tools=False)) == ["Edit", "Read", "Write"]
    assert names(wire_tools(resolve_tool_dialect(None), coding, custom_tools=False)) == [
        "edit_file",
        "read_file",
        "write_file",
    ]
    assert ToolKey("coding", "apply_patch") in CLAUDE_DIALECT.hidden


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
    """Codex models stop a session by the ID they were shown."""
    kill = _wire("kill_shell_command")

    assert kill.to_canonical({"session_id": 0x0123ABCD}) == {"handle": "shell:0123abcd", "force": False}
    assert kill.to_canonical({"session_id": 1, "force": True}) == {"handle": "shell:00000001", "force": True}
    assert kill.to_wire({"handle": "shell:0123abcd", "force": False}) == {"session_id": 0x0123ABCD, "force": False}
    assert _render(
        "kill_shell_command",
        "Terminated process 77 (SIGTERM sent). Use check_shell_command('shell:0123abcd') to confirm exit.",
    ) == (f"Terminated process 77 (SIGTERM sent). Use write_stdin(session_id={0x0123ABCD}) to confirm exit.")


def test_background_session_names_how_to_stop_it() -> None:
    """A session that keeps running says how to stop it with the tools Codex models see."""
    rendered = _render("exec_command", _format_background_handle_message(10, 77, "shell:0123abcd"))

    assert f"kill_shell_command(session_id={0x0123ABCD})" in rendered


def test_exec_command_yields_like_codex() -> None:
    """exec_command waits 10 seconds by default and at most 30, as Codex does."""
    to_canonical = _wire("exec_command").to_canonical

    assert to_canonical({"cmd": "ls"}) == {"args": "ls", "timeout": 10}
    assert to_canonical({"cmd": "ls", "yield_time_ms": 600000}) == {"args": "ls", "timeout": 30}


def test_kill_shell_command_call_dispatches_with_the_handle() -> None:
    """A Codex model's kill_shell_command(session_id) reaches the canonical function with its handle."""
    kill = _function("kill_shell_command", "shell")
    call = {
        "id": "c1",
        "type": "function",
        "function": {"name": "kill_shell_command", "arguments": json.dumps({"session_id": 0x0123ABCD})},
    }

    [translated], errors = canonical_tool_calls(CODEX_DIALECT, [call], {"kill_shell_command": kill})

    assert errors == []
    assert json.loads(translated["function"]["arguments"]) == {"handle": "shell:0123abcd", "force": False}


def test_stale_kill_names_the_session_id() -> None:
    """Stopping a session MindRoom no longer knows names the session ID the model used."""
    assert (
        _render("kill_shell_command", "Error: Unknown handle 'shell:0123abcd'")
        == f"Error: Unknown session ID {0x0123ABCD}"
    )


def test_default_waits_need_no_wire_record() -> None:
    """Calls that leave Codex's default yields unset translate losslessly, so history stores them once."""
    functions = {
        "run_shell_command": _function("run_shell_command", "shell"),
        "check_shell_command": _function("check_shell_command", "shell"),
    }
    calls = [
        {
            "id": "a",
            "type": "function",
            "function": {"name": "exec_command", "arguments": json.dumps({"cmd": "make test"})},
        },
        {
            "id": "b",
            "type": "function",
            "function": {"name": "write_stdin", "arguments": json.dumps({"session_id": 1})},
        },
    ]

    translated, errors = canonical_tool_calls(CODEX_DIALECT, calls, functions)

    assert errors == []
    assert [MINDROOM_WIRE_KEY in call for call in translated] == [False, False]
