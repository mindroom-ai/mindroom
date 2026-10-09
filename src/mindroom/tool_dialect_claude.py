"""Claude Code's tool names and argument shapes for MindRoom's canonical shell and coding functions.

Names, argument names, and argument types follow Claude Code's Bash, BashOutput, KillShell, Read, Edit,
and Write tools; the descriptions are MindRoom's own.
"""

from __future__ import annotations

import shlex
from typing import Any

from mindroom.custom_tools.coding import EDIT_NOT_FOUND_ERROR, parse_edit_multiple_matches_error, split_read_output
from mindroom.shell_execution import DEFAULT_RUN_TIMEOUT_SECONDS, parse_background_handle_message, parse_kill_message
from mindroom.tool_dialect_types import ToolDialect, WireFunction, milliseconds_to_seconds, wire_argument
from mindroom.tool_system.tool_access import ToolKey
from mindroom.tools.shell import WORKING_METHOD_NOTE, WORKSPACE_CWD_NOTE, split_cwd_prefix


def _object_schema(properties: dict[str, dict[str, Any]], *required: str) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def _render_kill(text: str) -> str:
    kill = parse_kill_message(text)
    if kill is None:
        return text
    action, pid, signal, handle = kill
    return f'{action} process {pid} ({signal} sent). Use BashOutput(bash_id="{handle}") to confirm exit.'


def _render_bash(text: str) -> str:
    _cwd, rest = split_cwd_prefix(text)
    background = parse_background_handle_message(rest)
    if background is None:
        return text
    handle = background.handle
    poll = f'Poll it with BashOutput(bash_id="{handle}") or stop it with KillShell(shell_id="{handle}").'
    if background.timeout > 0:
        status = (
            f"Command did not finish within {background.timeout:g}s and keeps running in the background "
            f"(PID {background.pid}) with ID: {handle}."
        )
    else:
        status = f"Command running in the background (PID {background.pid}) with ID: {handle}."
    return f"{text[: len(text) - len(rest)]}{status} {poll}"


def _bash_to_canonical(arguments: dict[str, Any]) -> dict[str, Any]:
    canonical: dict[str, Any] = {"args": wire_argument(arguments, "Bash", "command")}
    if wire_argument(arguments, "Bash", "run_in_background", kind=bool, required=False):
        canonical["timeout"] = 0
    elif (timeout := wire_argument(arguments, "Bash", "timeout", kind=float, required=False)) is not None:
        # Longer foreground waits outlast the worker proxy request; the command backgrounds instead.
        canonical["timeout"] = min(milliseconds_to_seconds(timeout, "Bash", "timeout"), DEFAULT_RUN_TIMEOUT_SECONDS)
    return canonical


def _bash_to_wire(canonical: dict[str, Any]) -> dict[str, Any]:
    args = canonical.get("args")
    command = shlex.join(args) if isinstance(args, list) else str(args or "")
    if isinstance(workdir := canonical.get("workdir"), str):
        command = f"cd {shlex.quote(workdir)} && {command}"
    wire: dict[str, Any] = {"command": command}
    timeout = canonical.get("timeout")
    if timeout == 0:
        wire["run_in_background"] = True
    elif isinstance(timeout, int | float):
        wire["timeout"] = int(timeout * 1000)
    return wire


def _read_to_canonical(arguments: dict[str, Any]) -> dict[str, Any]:
    canonical: dict[str, Any] = {"path": wire_argument(arguments, "Read", "file_path", "path")}
    if (offset := wire_argument(arguments, "Read", "offset", kind=int, required=False)) is not None:
        canonical["offset"] = max(offset, 1)
    if (limit := wire_argument(arguments, "Read", "limit", kind=int, required=False)) is not None:
        canonical["limit"] = limit
    return canonical


def _read_to_wire(canonical: dict[str, Any]) -> dict[str, Any]:
    wire: dict[str, Any] = {"file_path": canonical.get("path")}
    wire.update({name: canonical[name] for name in ("offset", "limit") if canonical.get(name) is not None})
    return wire


def _render_read(text: str) -> str:
    parsed = split_read_output(text)
    if parsed is None:
        return text
    lines, hint = parsed
    return "\n".join(f"{number}\t{line}" for number, line in lines) + hint


def _edit_to_canonical(arguments: dict[str, Any]) -> dict[str, Any]:
    canonical: dict[str, Any] = {
        "path": wire_argument(arguments, "Edit", "file_path", "path"),
        "old_text": wire_argument(arguments, "Edit", "old_string", "old_str"),
        "new_text": wire_argument(arguments, "Edit", "new_string", "new_str"),
    }
    if (replace_all := wire_argument(arguments, "Edit", "replace_all", kind=bool, required=False)) is not None:
        canonical["replace_all"] = replace_all
    return canonical


def _edit_to_wire(canonical: dict[str, Any]) -> dict[str, Any]:
    wire = {
        "file_path": canonical.get("path"),
        "old_string": canonical.get("old_text"),
        "new_string": canonical.get("new_text"),
    }
    if canonical.get("replace_all") is not None:
        wire["replace_all"] = canonical["replace_all"]
    return wire


def _render_edit(text: str) -> str:
    if text == EDIT_NOT_FOUND_ERROR:
        return "Error: String to replace not found in file."
    count = parse_edit_multiple_matches_error(text)
    if count is None:
        return text
    return (
        f"Error: Found {count} matches of the string to replace, but replace_all is false. "
        "Set replace_all to true or provide more context to make the match unique."
    )


def _write_to_canonical(arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": wire_argument(arguments, "Write", "file_path", "path"),
        "content": wire_argument(arguments, "Write", "content", "file_text", "file_content"),
    }


_BASH = WireFunction(
    key=ToolKey("shell", "run_shell_command"),
    wire_name="Bash",
    description=(
        "Run a bash command and return its output.\n"
        "- Every call starts a fresh non-login bash, so `cd` and exported variables do not carry over to the "
        "next call; chain dependent steps with `&&`.\n"
        "- `timeout` is in milliseconds (default and maximum 120000); a command still running then keeps running in the "
        "background and returns an ID for BashOutput and KillShell.\n"
        "- `run_in_background: true` starts the command in the background at once."
    ),
    parameters=_object_schema(
        {
            "command": {"type": "string", "description": "The command to execute"},
            "timeout": {
                "type": "number",
                "description": "Milliseconds to wait before the command moves to the background",
            },
            "description": {"type": "string", "description": "Short active-voice description of what the command does"},
            "run_in_background": {"type": "boolean", "description": "Start the command in the background at once"},
        },
        "command",
    ),
    to_canonical=_bash_to_canonical,
    to_wire=_bash_to_wire,
    render_result=_render_bash,
    carried_notes=(WORKSPACE_CWD_NOTE, WORKING_METHOD_NOTE),
)
_BASH_OUTPUT = WireFunction(
    key=ToolKey("shell", "check_shell_command"),
    wire_name="BashOutput",
    description="Return the status and output of a background command started by Bash.",
    parameters=_object_schema(
        {"bash_id": {"type": "string", "description": "The background command's ID"}},
        "bash_id",
    ),
    to_canonical=lambda arguments: {"handle": wire_argument(arguments, "BashOutput", "bash_id")},
    to_wire=lambda canonical: {"bash_id": canonical.get("handle")},
)
_KILL_SHELL = WireFunction(
    key=ToolKey("shell", "kill_shell_command"),
    wire_name="KillShell",
    description="Stop a background command started by Bash.",
    parameters=_object_schema(
        {"shell_id": {"type": "string", "description": "The background command's ID"}},
        "shell_id",
    ),
    to_canonical=lambda arguments: {"handle": wire_argument(arguments, "KillShell", "shell_id"), "force": False},
    to_wire=lambda canonical: {"shell_id": canonical.get("handle")},
    render_result=_render_kill,
)
_READ = WireFunction(
    key=ToolKey("coding", "read_file"),
    wire_name="Read",
    description=(
        "Read a file and return its lines numbered from 1, each number followed by a tab.\n"
        "- Reads up to 2000 lines by default; pass `offset` (the line to start from) and `limit` for larger files.\n"
        "- `file_path` may be absolute or relative to the working directory."
    ),
    parameters=_object_schema(
        {
            "file_path": {"type": "string", "description": "The path of the file to read"},
            "offset": {"type": "integer", "description": "The line number to start reading from"},
            "limit": {"type": "integer", "description": "The number of lines to read"},
        },
        "file_path",
    ),
    to_canonical=_read_to_canonical,
    to_wire=_read_to_wire,
    render_result=_render_read,
)
_EDIT = WireFunction(
    key=ToolKey("coding", "edit_file"),
    wire_name="Edit",
    description=(
        "Replace exact text in a file and return a diff of the change.\n"
        "- `old_string` must match the file without Read's line-number prefix (the number and tab) and must be "
        "unique unless `replace_all` is true.\n"
        "- Small whitespace and Unicode differences are tolerated."
    ),
    parameters=_object_schema(
        {
            "file_path": {"type": "string", "description": "The path of the file to modify"},
            "old_string": {"type": "string", "description": "The text to replace"},
            "new_string": {"type": "string", "description": "The text to replace it with"},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence of old_string"},
        },
        "file_path",
        "old_string",
        "new_string",
    ),
    to_canonical=_edit_to_canonical,
    to_wire=_edit_to_wire,
    render_result=_render_edit,
)
_WRITE = WireFunction(
    key=ToolKey("coding", "write_file"),
    wire_name="Write",
    description="Write a file, replacing it if it exists and creating missing parent directories. Prefer Edit for partial changes.",
    parameters=_object_schema(
        {
            "file_path": {"type": "string", "description": "The path of the file to write"},
            "content": {"type": "string", "description": "The full content to write"},
        },
        "file_path",
        "content",
    ),
    to_canonical=_write_to_canonical,
    to_wire=lambda canonical: {"file_path": canonical.get("path"), "content": canonical.get("content")},
)

CLAUDE_DIALECT = ToolDialect(
    name="claude",
    functions=(_BASH, _BASH_OUTPUT, _KILL_SHELL, _READ, _EDIT, _WRITE),
    hidden=frozenset({ToolKey("coding", "apply_patch")}),
)
