"""Codex CLI's tool names and argument shapes for MindRoom's canonical shell and coding functions.

Names, argument names, and argument types follow Codex's exec_command, write_stdin, and apply_patch
tools; edit_file and write_file are hidden because Codex models edit with apply_patch.
The apply_patch grammar is copied from, and its description adapted from, OpenAI Codex,
https://github.com/openai/codex at commit 7f2f4fd46e0f50798fd8d3436a8e14a5386d253b
(``codex-rs/core/assets/tools/apply_patch.lark``, ``codex-rs/core/src/tools/handlers/apply_patch_spec.rs``),
Copyright 2025 OpenAI, licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

import math
from typing import Any

from mindroom.shell_execution import (
    MAX_CHECK_WAIT_SECONDS,
    parse_background_handle_message,
    parse_check_status,
    parse_kill_message,
    parse_unknown_handle_error,
)
from mindroom.tool_dialects.types import (
    DialectArgumentError,
    ToolDialect,
    WireFunction,
    milliseconds_to_seconds,
    object_schema,
    shell_command_text,
    wire_argument,
)
from mindroom.tool_system.tool_access import ToolKey
from mindroom.tools.shell import WORKING_METHOD_NOTE, WORKSPACE_CWD_NOTE, split_cwd_prefix

_HANDLE_PREFIX = "shell:"
# Codex yields a command after 10 seconds by default, between 250 ms and 30 seconds, and waits at least
# 5 seconds on an empty poll; MindRoom caps a poll's wait at MAX_CHECK_WAIT_SECONDS.
_EMPTY_POLL_WAIT_SECONDS = (5, MAX_CHECK_WAIT_SECONDS)
_YIELD_MS = (250, 10_000, 30_000)
_APPLY_PATCH_GRAMMAR = """start: begin_patch hunk+ end_patch
begin_patch: "*** Begin Patch" LF
end_patch: "*** End Patch" LF?

hunk: add_hunk | delete_hunk | update_hunk
add_hunk: "*** Add File: " filename LF add_line+
delete_hunk: "*** Delete File: " filename LF
update_hunk: "*** Update File: " filename LF change_move? change?

filename: /(.+)/
add_line: "+" /(.*)/ LF -> line

change_move: "*** Move to: " filename LF
change: (change_context | change_line)+ eof_line?
change_context: ("@@" | "@@ " /(.+)/) LF
change_line: ("+" | "-" | " ") /(.*)/ LF
eof_line: "*** End of File" LF

%import common.LF
"""


def _session_id(handle: str) -> int | None:
    digits = handle.removeprefix(_HANDLE_PREFIX)
    if not handle.startswith(_HANDLE_PREFIX) or not digits:
        return None
    try:
        return int(digits, 16)
    except ValueError:
        return None


def _handle(session_id: int) -> str:
    return f"{_HANDLE_PREFIX}{session_id:08x}"


def _render_exec(text: str) -> str:
    prefix, rest = split_cwd_prefix(text)
    background = parse_background_handle_message(rest)
    if background is None:
        return text
    session_id = _session_id(background.handle)
    return (
        f"{prefix}Wall time: {background.timeout:g} seconds\n"
        f"Process running with session ID {session_id} (PID {background.pid}); "
        f"poll it with write_stdin or stop it with kill_shell_command(session_id={session_id})\nOutput:\n"
    )


def _render_poll(text: str) -> str:
    if (unknown := _render_unknown_handle(text)) is not None:
        return unknown
    status = parse_check_status(text)
    if status is None:
        return text
    state = f"Process running (PID {status.pid})" if status.running else f"Process exited with code {status.exit_code}"
    stderr = f"\nStderr:\n{status.stderr}" if status.stderr else ""
    return f"Wall time: {status.elapsed:g} seconds\n{state}\nOutput:\n{status.output}{stderr}"


def _render_unknown_handle(text: str) -> str | None:
    handle = parse_unknown_handle_error(text)
    return f"Error: Unknown session ID {_session_id(handle)}" if handle is not None else None


def _render_kill(text: str) -> str:
    if (unknown := _render_unknown_handle(text)) is not None:
        return unknown
    kill = parse_kill_message(text)
    if kill is None:
        return text
    action, pid, signal, handle = kill
    return f"{action} process {pid} ({signal} sent). Use write_stdin(session_id={_session_id(handle)}) to confirm exit."


def _exec_to_canonical(arguments: dict[str, Any]) -> dict[str, Any]:
    canonical: dict[str, Any] = {"args": wire_argument(arguments, "exec_command", "cmd")}
    if (workdir := wire_argument(arguments, "exec_command", "workdir", required=False)) is not None:
        canonical["workdir"] = workdir
    minimum, default, maximum = _YIELD_MS
    yield_ms = wire_argument(arguments, "exec_command", "yield_time_ms", kind=float, required=False)
    clamped = default if yield_ms is None else min(max(yield_ms, minimum), maximum)
    canonical["timeout"] = milliseconds_to_seconds(clamped, "exec_command", "yield_time_ms")
    # Output length follows the canonical tool's tail and byte limits, so max_output_tokens is not forwarded.
    return canonical


def _exec_to_wire(canonical: dict[str, Any]) -> dict[str, Any]:
    wire: dict[str, Any] = {"cmd": shell_command_text(canonical.get("args"))}
    if canonical.get("workdir") is not None:
        wire["workdir"] = canonical["workdir"]
    _minimum, default, _maximum = _YIELD_MS
    # Omitting Codex's default keeps a call that left it unset lossless, so history stores it once.
    if isinstance(timeout := canonical.get("timeout"), int | float) and timeout * 1000 != default:
        wire["yield_time_ms"] = int(timeout * 1000)
    return wire


def _write_stdin_to_canonical(arguments: dict[str, Any]) -> dict[str, Any]:
    session_id = wire_argument(arguments, "write_stdin", "session_id", kind=int)
    if wire_argument(arguments, "write_stdin", "chars", required=False):
        msg = "write_stdin cannot send input; pass empty chars to poll, or stop a session with kill_shell_command"
        raise DialectArgumentError(msg)
    yield_ms = wire_argument(arguments, "write_stdin", "yield_time_ms", kind=float, required=False)
    minimum, maximum = _EMPTY_POLL_WAIT_SECONDS
    wait = minimum if yield_ms is None else min(max(math.ceil(yield_ms / 1000), minimum), maximum)
    return {"handle": _handle(session_id), "wait": wait}


def _write_stdin_to_wire(canonical: dict[str, Any]) -> dict[str, Any]:
    handle = str(canonical.get("handle", ""))
    session_id = _session_id(handle)
    wire: dict[str, Any] = {"session_id": session_id if session_id is not None else handle}
    if isinstance(wait := canonical.get("wait"), int | float) and wait != _EMPTY_POLL_WAIT_SECONDS[0]:
        wire["yield_time_ms"] = int(wait * 1000)
    return wire


_EXEC_COMMAND = WireFunction(
    key=ToolKey("shell", "run_shell_command"),
    wire_name="exec_command",
    description=(
        "Runs a shell command and returns its output, or a session ID when it is still running after "
        "`yield_time_ms`.\n"
        "- Every call starts a fresh non-login bash in `workdir`, which defaults to the working directory.\n"
        "- Poll a running session with write_stdin and empty `chars`."
    ),
    parameters=object_schema(
        {
            "cmd": {"type": "string", "description": "Shell command to execute."},
            "max_output_tokens": {
                "type": "number",
                "description": "Accepted for compatibility; MindRoom limits output itself.",
            },
            "workdir": {
                "type": "string",
                "description": "Working directory for the command. Defaults to the working directory.",
            },
            "yield_time_ms": {
                "type": "number",
                "description": "Wait before returning a session ID for a command still running. Defaults to 10000 ms, at most 30000 ms.",
            },
        },
        "cmd",
    ),
    to_canonical=_exec_to_canonical,
    to_wire=_exec_to_wire,
    render_result=_render_exec,
    carried_notes=(WORKSPACE_CWD_NOTE, WORKING_METHOD_NOTE),
)
_WRITE_STDIN = WireFunction(
    key=ToolKey("shell", "check_shell_command"),
    wire_name="write_stdin",
    description=(
        "Polls a running exec_command session and returns its output once it finishes or `yield_time_ms` "
        "elapses. Writing input is not supported, so `chars` must be empty."
    ),
    parameters=object_schema(
        {
            "chars": {"type": "string", "description": "Must be empty; writing input is not supported."},
            "session_id": {"type": "number", "description": "Identifier of the running exec_command session."},
            "yield_time_ms": {
                "type": "number",
                "description": "Wait up to this long for the session to finish. Defaults to 5000 ms, at most 60000 ms.",
            },
        },
        "session_id",
    ),
    to_canonical=_write_stdin_to_canonical,
    to_wire=_write_stdin_to_wire,
    render_result=_render_poll,
)
_APPLY_PATCH = WireFunction(
    key=ToolKey("coding", "apply_patch"),
    wire_name="apply_patch",
    description=(
        "Use the `apply_patch` tool to edit files. The patch is a file-oriented diff:\n\n"
        "*** Begin Patch\n[one or more file sections]\n*** End Patch\n\n"
        "Each file section starts with one header:\n"
        "*** Add File: <path> - create a file; every following line is a + line with its contents.\n"
        "*** Delete File: <path> - remove an existing file; nothing follows.\n"
        "*** Update File: <path> - change an existing file, optionally followed by *** Move to: <new path> "
        "to rename it.\n\n"
        "An update holds hunks introduced by @@, optionally followed by a line such as a class or function "
        "definition that locates the hunk. In a hunk, lines starting with a space are context, - lines are "
        "removed, and + lines are added. Show 3 lines of context above and below each change unless @@ lines "
        "already make the location unique, and end a hunk at the end of a file with *** End of File. "
        "Paths are relative to the working directory."
    ),
    parameters=object_schema(
        {"input": {"type": "string", "description": "The entire contents of the apply_patch command"}},
        "input",
    ),
    to_canonical=lambda arguments: {"input": wire_argument(arguments, "apply_patch", "input")},
    to_wire=lambda canonical: {"input": canonical.get("input")},
    custom_format={"type": "grammar", "syntax": "lark", "definition": _APPLY_PATCH_GRAMMAR},
)

_KILL_SHELL_COMMAND = WireFunction(
    key=ToolKey("shell", "kill_shell_command"),
    wire_name="kill_shell_command",
    description="Stops a running exec_command session.",
    parameters=object_schema(
        {
            "force": {"type": "boolean", "description": "Send SIGKILL at once instead of SIGTERM."},
            "session_id": {"type": "number", "description": "Identifier of the running exec_command session."},
        },
        "session_id",
    ),
    to_canonical=lambda arguments: {
        "handle": _handle(wire_argument(arguments, "kill_shell_command", "session_id", kind=int)),
        "force": bool(wire_argument(arguments, "kill_shell_command", "force", kind=bool, required=False)),
    },
    to_wire=lambda canonical: {
        "session_id": _session_id(str(canonical.get("handle", ""))),
        "force": bool(canonical.get("force")),
    },
    render_result=_render_kill,
)

CODEX_DIALECT = ToolDialect(
    name="codex",
    functions=(_EXEC_COMMAND, _WRITE_STDIN, _KILL_SHELL_COMMAND, _APPLY_PATCH),
    hidden=frozenset({ToolKey("coding", "edit_file"), ToolKey("coding", "write_file")}),
)
