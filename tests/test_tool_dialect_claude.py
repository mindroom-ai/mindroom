"""Tests for the Claude Code tool dialect."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from agno.agent import Agent
from agno.tools.toolkit import Toolkit
from anthropic import AsyncAnthropic

from mindroom.agents import _set_toolkit_approval_origin
from mindroom.agno_compat_tool_dialect import install_tool_dialect
from mindroom.anthropic_claude import MindRoomAnthropicClaude
from mindroom.config.models import ModelConfig
from mindroom.custom_tools.coding import EDIT_NOT_FOUND_ERROR, _format_read_output
from mindroom.shell_execution import _format_background_handle_message
from mindroom.tool_dialect_claude import CLAUDE_DIALECT
from mindroom.tool_dialect_types import DialectArgumentError, WireFunction
from mindroom.tool_dialects import resolve_tool_dialect
from mindroom.tools.shell import WORKING_METHOD_NOTE, WORKSPACE_CWD_NOTE


def _wire(name: str) -> WireFunction:
    return next(function for function in CLAUDE_DIALECT.functions if function.wire_name == name)


def _render(name: str, text: str) -> str:
    render = _wire(name).render_result
    assert render is not None
    return render(text)


def test_bash_maps_command_timeout_background() -> None:
    """Bash takes milliseconds and a background flag; canonical shell takes seconds, where 0 backgrounds at once."""
    to_canonical = _wire("Bash").to_canonical

    assert to_canonical({"command": "ls", "description": "List files"}) == {"args": "ls"}
    assert to_canonical({"command": "make", "timeout": 1500}) == {"args": "make", "timeout": 2}
    assert to_canonical({"command": "serve", "run_in_background": True, "timeout": 9000}) == {
        "args": "serve",
        "timeout": 0,
    }
    with pytest.raises(DialectArgumentError, match="Bash requires command"):
        to_canonical({"cmd": "ls"})
    with pytest.raises(DialectArgumentError, match="timeout"):
        to_canonical({"command": "ls", "timeout": "soon"})


def test_bash_output_and_kill_shell_map_handles() -> None:
    """BashOutput and KillShell name the background ID the way Claude Code does."""
    assert _wire("BashOutput").to_canonical({"bash_id": "shell:0123abcd"}) == {"handle": "shell:0123abcd"}
    assert _wire("KillShell").to_canonical({"shell_id": "shell:0123abcd"}) == {
        "handle": "shell:0123abcd",
        "force": False,
    }


def test_read_offset_zero_and_tab_rendering() -> None:
    """Read starts at line 1 for offset 0 and renders line numbers followed by a tab."""
    content = "\n".join(f"line {number}" for number in range(1, 13))

    assert _wire("Read").to_canonical({"file_path": "a.py", "offset": 0, "limit": 5}) == {
        "path": "a.py",
        "offset": 1,
        "limit": 5,
    }
    assert _render("Read", _format_read_output(content, 9, 2)) == (
        "9\tline 9\n10\tline 10\n\n[Showing lines 9-10 of 12. Use offset=11 to continue.]"
    )


def test_edit_and_write_map_arguments() -> None:
    """Edit and Write use Claude Code's argument names."""
    assert _wire("Edit").to_canonical(
        {"file_path": "a.py", "old_string": "x", "new_string": "y", "replace_all": True},
    ) == {"path": "a.py", "old_text": "x", "new_text": "y", "replace_all": True}
    assert _wire("Edit").to_canonical({"file_path": "a.py", "old_string": "x", "new_string": ""}) == {
        "path": "a.py",
        "old_text": "x",
        "new_text": "",
    }
    assert _wire("Write").to_canonical({"file_path": "a.py", "content": "z"}) == {"path": "a.py", "content": "z"}


def test_text_editor_aliases_are_accepted() -> None:
    """Text-editor argument names Claude also knows reach the same canonical calls."""
    assert _wire("Read").to_canonical({"path": "a.py"}) == {"path": "a.py"}
    assert _wire("Edit").to_canonical({"path": "a.py", "old_str": "x", "new_str": "y"}) == {
        "path": "a.py",
        "old_text": "x",
        "new_text": "y",
    }
    assert _wire("Write").to_canonical({"path": "a.py", "file_text": "z"}) == {"path": "a.py", "content": "z"}
    assert _wire("Write").to_canonical({"file_path": "a.py", "file_content": "z"}) == {"path": "a.py", "content": "z"}


@pytest.mark.parametrize(
    ("wire_name", "canonical"),
    [
        ("Bash", {"args": "ls -la", "timeout": 3}),
        ("Bash", {"args": "serve", "timeout": 0}),
        ("BashOutput", {"handle": "shell:0123abcd"}),
        ("KillShell", {"handle": "shell:0123abcd", "force": False}),
        ("Read", {"path": "a.py", "offset": 4, "limit": 9}),
        ("Edit", {"path": "a.py", "old_text": "x", "new_text": "y", "replace_all": True}),
        ("Write", {"path": "a.py", "content": "z"}),
    ],
)
def test_round_trip_for_every_function(wire_name: str, canonical: dict[str, object]) -> None:
    """Canonical history renders into wire arguments that translate back to the same call."""
    wire = _wire(wire_name)

    assert wire.to_canonical(wire.to_wire(canonical)) == canonical


def test_argv_history_renders_as_command_string() -> None:
    """A canonical argv list from older history renders as one shell-quoted Bash command."""
    assert _wire("Bash").to_wire({"args": ["echo", "a b"]}) == {"command": "echo 'a b'"}


def test_background_handle_renders_claude_wording() -> None:
    """A command moved to the background tells the model to use BashOutput and KillShell."""
    message = "[cwd: /w]\n" + _format_background_handle_message(120, 77, "shell:0123abcd")
    immediate = _format_background_handle_message(0, 78, "shell:4567cdef")

    assert _render("Bash", message) == (
        "[cwd: /w]\nCommand did not finish within 120s and keeps running in the background (PID 77) with ID: "
        'shell:0123abcd. Poll it with BashOutput(bash_id="shell:0123abcd") or stop it with '
        'KillShell(shell_id="shell:0123abcd").'
    )
    assert _render("Bash", immediate) == (
        'Command running in the background (PID 78) with ID: shell:4567cdef. Poll it with BashOutput(bash_id="'
        'shell:4567cdef") or stop it with KillShell(shell_id="shell:4567cdef").'
    )
    assert _render(
        "KillShell",
        "Terminated process 77 (SIGTERM sent). Use check_shell_command('shell:0123abcd') to confirm exit.",
    ) == ('Terminated process 77 (SIGTERM sent). Use BashOutput(bash_id="shell:0123abcd") to confirm exit.')


def test_edit_errors_render_claude_wording() -> None:
    """Edit match errors use Claude Code's wording, including the replace_all hint."""
    multiple = (
        "Error: old_text matches 3 locations. "
        "Provide more context to make the match unique, or set replace_all to replace every match."
    )

    assert _render("Edit", EDIT_NOT_FOUND_ERROR) == "Error: String to replace not found in file."
    assert _render("Edit", multiple) == (
        "Error: Found 3 matches of the string to replace, but replace_all is false. "
        "Set replace_all to true or provide more context to make the match unique."
    )


def test_unrelated_results_pass_through() -> None:
    """Output that is not a fixed MindRoom template stays byte for byte."""
    for name, text in [
        ("Bash", "[cwd: /w]\nhello\n"),
        ("Read", "Error: File not found: a.py"),
        ("Edit", "Applied edit"),
    ]:
        assert _render(name, text) == text


def test_resolve_tool_dialect_returns_claude_for_anthropic() -> None:
    """Claude models get the Claude dialect."""
    assert resolve_tool_dialect(ModelConfig(provider="anthropic", id="claude-sonnet-5-5")) is CLAUDE_DIALECT


def test_bash_carries_mindroom_shell_notes() -> None:
    """Bash keeps the workspace and working-method notes MindRoom adds to its shell tool."""
    assert _wire("Bash").carried_notes == (WORKSPACE_CWD_NOTE, WORKING_METHOD_NOTE)


_USAGE = {"input_tokens": 10, "output_tokens": 1}


def _claude_response(blocks: list[dict[str, Any]], stop_reason: str) -> dict[str, Any]:
    return {
        "id": f"msg_{stop_reason}",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": _USAGE,
    }


@pytest.mark.asyncio
async def test_thinking_replay_keeps_wire_call_verbatim() -> None:
    """A signed thinking block and its Bash call replay exactly as Claude sent them."""
    executions: list[str] = []
    thinking = {"type": "thinking", "thinking": "List first.", "signature": "signed"}
    tool_use = {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "Bash",
        "input": {"command": "ls", "description": "List files"},
    }
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, json=_claude_response([thinking, tool_use], "tool_use"))
        return httpx.Response(200, json=_claude_response([{"type": "text", "text": "Done"}], "end_turn"))

    def run_shell_command(args: str) -> str:
        """Run a shell command."""
        executions.append(args)
        return "a.txt"

    toolkit = Toolkit(name="shell", tools=[run_shell_command])
    _set_toolkit_approval_origin(toolkit, "shell")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        model = MindRoomAnthropicClaude(
            id="claude-sonnet-5-5",
            async_client=AsyncAnthropic(api_key="test-key", http_client=http_client),
        )
        install_tool_dialect(model, CLAUDE_DIALECT)
        await Agent(model=model, tools=[toolkit], telemetry=False).arun("List the files.")

    assert executions == ["ls"]
    assert [tool["name"] for tool in requests[0]["tools"]] == ["Bash"]
    assistant = next(message for message in requests[1]["messages"] if message["role"] == "assistant")
    assert assistant["content"] == [thinking, tool_use]
