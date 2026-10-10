"""Tests for wire translation between canonical MindRoom tools and model-family tool dialects."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from agno.models.message import Message
from agno.tools.function import Function
from structlog.testing import capture_logs

from mindroom.config.models import ModelConfig
from mindroom.tool_dialects.translation import (
    _resolve_tool_dialect_name,
    _warn_name_collision,
    canonical_tool_calls,
    wire_function_name,
    wire_messages,
    wire_tools,
)
from mindroom.tool_dialects.types import DialectArgumentError, ToolDialect, WireFunction
from mindroom.tool_system.output_files import OUTPUT_PATH_ARGUMENT
from mindroom.tool_system.tool_access import ToolKey

_RUN = ToolKey("shell", "run_shell_command")
_EDIT = ToolKey("coding", "edit_file")
_PATCH = ToolKey("coding", "apply_patch")


def _run_to_canonical(arguments: dict[str, Any]) -> dict[str, Any]:
    if "cmd" not in arguments:
        msg = "Run requires cmd"
        raise DialectArgumentError(msg)
    return {"args": arguments["cmd"]}


_TOY = ToolDialect(
    name="claude",
    functions=(
        WireFunction(
            key=_RUN,
            wire_name="Run",
            description="Run a command.",
            parameters={"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
            to_canonical=_run_to_canonical,
            to_wire=lambda arguments: {"cmd": arguments["args"]},
        ),
    ),
    replaced={_EDIT: (_PATCH,)},
)


def _function(name: str, toolkit: str | None) -> Function:
    function = Function(
        name=name,
        description=f"canonical {name}",
        parameters={"type": "object", "properties": {"args": {"type": "string"}}},
        strict=True,
    )
    function.owning_toolkit = toolkit
    return function


def _call(identifier: str, name: str, arguments: dict[str, Any], **extra: object) -> dict[str, Any]:
    return {
        "id": identifier,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
        **extra,
    }


def _names(tools: list[Function | dict[str, Any]]) -> list[str]:
    return [
        tool.name
        if isinstance(tool, Function)
        else tool["function"]["name"]
        if tool["type"] == "function"
        else tool["name"]
        for tool in tools
    ]


@pytest.mark.parametrize(
    ("provider", "model_id", "expected"),
    [
        ("anthropic", "claude-opus-5-5", "claude"),
        ("vertexai_claude", "claude-sonnet-5-5", "claude"),
        ("bedrock_claude", "anthropic.claude-sonnet-5-5", "claude"),
        ("openrouter", "anthropic/claude-sonnet-5.5", "claude"),
        ("openai", "gpt-6-astra", "mindroom"),
        ("azure", "gpt-6.1-sol", "mindroom"),
        ("codex", "gpt-6.1-sol", "mindroom"),
        ("openai-codex", "gpt-6-luna", "mindroom"),
        ("openrouter", "openai/gpt-6-astra", "mindroom"),
        ("openai", "qwen3.8:27b", "mindroom"),
        ("openrouter", "z-ai/glm-5.3", "mindroom"),
        ("google", "gemini-3.8-flash", "mindroom"),
        ("ollama", "qwen3.8:27b", "mindroom"),
    ],
)
def test_auto_resolution_table(provider: str, model_id: str, expected: str) -> None:
    """Auto picks the dialect from the provider and, where providers serve many families, the model ID."""
    assert _resolve_tool_dialect_name(ModelConfig(provider=provider, id=model_id)) == expected


@pytest.mark.parametrize("setting", ["mindroom", "claude", "codex"])
def test_explicit_tool_dialect_overrides_auto(setting: str) -> None:
    """An explicit tool_dialect wins over provider detection."""
    config = ModelConfig(provider="ollama", id="qwen3.8:27b", tool_dialect=setting)

    assert _resolve_tool_dialect_name(config) == setting


def test_wire_tools_renames_and_hides() -> None:
    """Mapped functions become wire definitions, replaced ones vanish beside their replacement, others stay."""
    ls = _function("ls", "coding")
    tools = [
        _function("run_shell_command", "shell"),
        _function("edit_file", "coding"),
        ls,
        _function("apply_patch", "coding"),
    ]

    presented = wire_tools(_TOY, tools, custom_tools=False)

    assert _names(presented) == ["Run", "ls", "apply_patch"]
    run = presented[0]
    assert isinstance(run, dict)
    assert run["function"]["description"] == "Run a command."
    assert run["function"]["parameters"]["required"] == ["cmd"]
    assert "strict" not in run["function"]
    assert presented[1] is ls


def test_wire_function_name_maps_only_dialect_functions() -> None:
    """Deferred-tool names follow the dialect for mapped functions and stay canonical otherwise."""
    assert wire_function_name(_TOY, "shell", "run_shell_command", ()) == "Run"
    assert wire_function_name(_TOY, "coding", "ls", ()) == "ls"
    assert wire_function_name(_TOY, "shell", "run_shell_command", {"Run"}) == "run_shell_command"


def test_wire_tools_skips_function_with_other_owner() -> None:
    """A same-named function from another toolkit is never presented as the dialect tool."""
    presented = wire_tools(_TOY, [_function("run_shell_command", "mcp_server")], custom_tools=False)

    assert _names(presented) == ["run_shell_command"]


def test_wire_tools_present_preset_owned_functions() -> None:
    """A canonical function a preset such as openclaw_compat brought in is presented in the dialect."""
    presented = wire_tools(_TOY, [_function("run_shell_command", "openclaw_compat")], custom_tools=False)

    assert _names(presented) == ["Run"]


def test_wire_name_collision_keeps_canonical() -> None:
    """A wire name already used by another tool leaves the mapped function canonical."""
    tools = [_function("run_shell_command", "shell"), _function("Run", "mcp_server")]

    assert _names(wire_tools(_TOY, tools, custom_tools=False)) == ["run_shell_command", "Run"]


def test_wire_name_collision_warns_once() -> None:
    """A collision is logged once, however many requests present the same tools."""
    _warn_name_collision.cache_clear()
    tools = [_function("run_shell_command", "shell"), _function("Run", "mcp")]

    with capture_logs() as logs:
        wire_tools(_TOY, tools, custom_tools=False)
        wire_tools(_TOY, tools, custom_tools=False)

    assert sum("collides" in entry["event"] for entry in logs) == 1


def test_canonical_tool_calls_translate_wire_calls() -> None:
    """A wire call becomes the canonical call with canonical arguments."""
    functions = {"run_shell_command": _function("run_shell_command", "shell")}
    call = _call("call_1", "Run", {"cmd": "ls -la"}, call_id="call_1")

    translated, errors = canonical_tool_calls(_TOY, [call], functions)

    assert errors == []
    assert translated == [
        {
            "id": "call_1",
            "call_id": "call_1",
            "type": "function",
            "function": {"name": "run_shell_command", "arguments": json.dumps({"args": "ls -la"})},
        },
    ]


def test_canonical_tool_calls_accept_preset_owned_functions() -> None:
    """A canonical function an authored preset brought in is still the dialect's function."""
    functions = {"run_shell_command": _function("run_shell_command", "openclaw_compat")}

    [translated], errors = canonical_tool_calls(_TOY, [_call("a", "Run", {"cmd": "ls"})], functions)

    assert errors == []
    assert translated["function"]["name"] == "run_shell_command"


def test_canonical_tool_calls_leave_existing_and_foreign_names() -> None:
    """Calls that already name a real function, or no mapped one, pass through unchanged."""
    functions = {"ls": _function("ls", "coding"), "Run": _function("Run", "mcp_server")}
    calls = [_call("a", "ls", {}), _call("b", "Run", {"cmd": "x"}), _call("c", "Unknown", {})]

    translated, errors = canonical_tool_calls(_TOY, calls, functions)

    assert translated == calls
    assert errors == []


def test_untranslatable_call_returns_tool_call_error() -> None:
    """Arguments the dialect cannot translate become a tool error for that exact call, kept as sent."""
    functions = {"run_shell_command": _function("run_shell_command", "shell")}
    calls = [
        _call("bad", "Run", {"command": "ls"}),
        _call("json", "Run", {}) | {"function": {"name": "Run", "arguments": "{"}},
    ]

    translated, errors = canonical_tool_calls(_TOY, calls, functions)

    assert translated == calls
    assert [(error.call is calls[index], error.name) for index, error in enumerate(errors)] == [
        (True, "Run"),
        (True, "Run"),
    ]
    assert errors[0].message == "Error: Run requires cmd"
    assert errors[1].message.startswith("Error: Invalid JSON arguments for Run")


def _assistant(*calls: dict[str, Any]) -> Message:
    return Message(role="assistant", tool_calls=list(calls))


_PRESENTED = [
    {"type": "function", "function": {"name": "Run", "parameters": _TOY.functions[0].parameters}},
    {"type": "function", "function": {"name": "ls", "parameters": {"type": "object", "properties": {}}}},
]


def _canonical_only(name: str) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": name, "parameters": {"type": "object", "properties": {}}}}]


def test_wire_messages_translate_canonical_calls_by_name() -> None:
    """Stored canonical calls render through the active dialect."""
    message = _assistant(_call("a", "run_shell_command", {"args": "pwd"}))

    [rendered] = wire_messages(_TOY, [message], _PRESENTED)

    assert rendered.tool_calls == [
        {"id": "a", "type": "function", "function": {"name": "Run", "arguments": json.dumps({"cmd": "pwd"})}},
    ]


def test_translated_arguments_keep_non_ascii_text_as_written() -> None:
    """Canonical and rendered arguments keep non-ASCII text unescaped, the way models write it."""
    functions = {"run_shell_command": _function("run_shell_command", "shell")}

    [translated], _errors = canonical_tool_calls(_TOY, [_call("a", "Run", {"cmd": "echo café"})], functions)
    [rendered] = wire_messages(_TOY, [_assistant(_call("b", "run_shell_command", {"args": "echo café"}))], _PRESENTED)

    assert translated["function"]["arguments"] == '{"args": "echo café"}'
    assert rendered.tool_calls is not None
    assert rendered.tool_calls[0]["function"]["arguments"] == '{"cmd": "echo café"}'


def test_wire_messages_render_only_tools_presented_in_wire_form() -> None:
    """Calls and results of a same-named tool the request presents canonically stay canonical."""
    call = _call("a", "run_shell_command", {"args": "x"})
    result = Message(role="tool", tool_call_id="a", tool_name="run_shell_command", content="ran check_shell_command")

    rendered = wire_messages(_TOY, [_assistant(call), result], _canonical_only("run_shell_command"))

    assert rendered[0].tool_calls == [call]
    assert rendered[1] is result


def test_foreign_tool_with_a_wire_name_gets_no_translated_history() -> None:
    """A foreign tool that only shares a wire name does not see canonical calls rendered as its own."""
    call = _call("a", "run_shell_command", {"args": "ls"})
    schema = {"type": "object", "properties": {"host": {"type": "string"}, "command": {"type": "string"}}}
    foreign = [{"type": "function", "function": {"name": "Run", "parameters": schema}}]

    [rendered] = wire_messages(_TOY, [_assistant(call)], foreign)

    assert rendered.tool_calls == [call]


def test_wire_messages_leaves_unmapped_calls() -> None:
    """Calls the active dialect does not map keep their canonical form."""
    message = _assistant(_call("a", "apply_patch", {"input": "*** Begin Patch"}))

    [rendered] = wire_messages(_TOY, [message], _PRESENTED)

    assert rendered is message


def test_wire_messages_does_not_mutate_inputs() -> None:
    """Rendering copies the messages it changes and leaves stored history canonical."""
    message = _assistant(_call("a", "run_shell_command", {"args": "ls"}))
    result = Message(role="tool", tool_call_id="a", tool_name="run_shell_command", content="a.txt")

    rendered = wire_messages(_TOY, [message, result], _PRESENTED)

    assert message.tool_calls is not None
    assert message.tool_calls[0]["function"]["name"] == "run_shell_command"
    assert result.tool_name == "run_shell_command"
    assert rendered[0] is not message
    assert (rendered[1].content, rendered[1].tool_name) == ("a.txt", "Run")


def test_wire_definition_carries_notes_present_on_the_canonical_function() -> None:
    """Notes MindRoom adds to a canonical description follow it into the wire description."""
    dialect = ToolDialect(name="claude", functions=(replace(_TOY.functions[0], carried_notes=("Note A.", "Note B.")),))
    function = _function("run_shell_command", "shell")
    function.description = "canonical run\n\nNote A."

    [presented] = wire_tools(dialect, [function], custom_tools=False)

    assert isinstance(presented, dict)
    assert presented["function"]["description"] == "Run a command.\n\nNote A."


def test_wire_name_equal_to_canonical_name_is_not_a_collision() -> None:
    """A dialect may keep a function's own name, as Codex does for apply_patch, and still present it in wire form."""
    patch = replace(
        _TOY.functions[0],
        key=ToolKey("coding", "apply_patch"),
        wire_name="apply_patch",
        custom_format={"type": "grammar"},
    )
    dialect = ToolDialect(name="codex", functions=(patch,))

    [presented] = wire_tools(dialect, [_function("apply_patch", "coding")], custom_tools=True)

    assert presented == {
        "type": "custom",
        "name": "apply_patch",
        "description": "Run a command.",
        "format": {"type": "grammar"},
    }


def test_output_path_argument_survives_translation() -> None:
    """The MindRoom-managed output-path argument stays offered, reaches the canonical call, and renders back."""
    function = _function("run_shell_command", "shell")
    function.parameters = {
        "type": "object",
        "properties": {"args": {"type": "string"}, OUTPUT_PATH_ARGUMENT: {"type": "string", "description": "save"}},
    }
    call = _call("a", "Run", {"cmd": "ls -R", OUTPUT_PATH_ARGUMENT: "out.txt"})

    [presented] = wire_tools(_TOY, [function], custom_tools=False)
    [translated], _errors = canonical_tool_calls(_TOY, [call], {"run_shell_command": function})
    [rendered] = wire_messages(_TOY, [_assistant(translated)], _PRESENTED)

    assert isinstance(presented, dict)
    assert presented["function"]["parameters"]["properties"][OUTPUT_PATH_ARGUMENT]["description"] == "save"
    assert json.loads(translated["function"]["arguments"]) == {"args": "ls -R", OUTPUT_PATH_ARGUMENT: "out.txt"}
    assert rendered.tool_calls is not None
    assert json.loads(rendered.tool_calls[0]["function"]["arguments"]) == {
        "cmd": "ls -R",
        OUTPUT_PATH_ARGUMENT: "out.txt",
    }


def test_identity_named_functions_keep_their_results_when_presented() -> None:
    """A dialect function that keeps its canonical name passes its results through unchanged."""
    same_name = replace(_TOY.functions[0], wire_name="run_shell_command")
    dialect = ToolDialect(name="codex", functions=(same_name,))
    result = Message(role="tool", tool_call_id="a", tool_name="run_shell_command", content="done")

    ours = {"type": "function", "function": {"name": "run_shell_command", "parameters": same_name.parameters}}
    [rendered] = wire_messages(dialect, [result], [ours])

    assert rendered is result


def test_same_named_dialect_function_translates_its_arguments() -> None:
    """A dialect function that keeps its canonical name still translates the arguments the model sends."""
    same_name = replace(_TOY.functions[0], wire_name="run_shell_command")
    dialect = ToolDialect(name="codex", functions=(same_name,))
    functions = {"run_shell_command": _function("run_shell_command", "shell")}

    [translated], errors = canonical_tool_calls(dialect, [_call("a", "run_shell_command", {"cmd": "ls"})], functions)
    [foreign], _ = canonical_tool_calls(
        dialect,
        [_call("b", "run_shell_command", {"cmd": "ls"})],
        {"run_shell_command": _function("run_shell_command", "mcp_server")},
    )

    assert errors == []
    assert json.loads(translated["function"]["arguments"]) == {"args": "ls"}
    assert json.loads(foreign["function"]["arguments"]) == {"cmd": "ls"}


def test_foreign_same_named_tool_history_stays_untouched() -> None:
    """A same-named tool from another server, presented with its own schema, keeps its recorded arguments."""
    same_name = replace(_TOY.functions[0], wire_name="run_shell_command")
    dialect = ToolDialect(name="codex", functions=(same_name,))
    foreign = {
        "type": "function",
        "function": {
            "name": "run_shell_command",
            "parameters": {"type": "object", "properties": {"script": {"type": "string"}}},
        },
    }
    ours = {"type": "function", "function": {"name": "run_shell_command", "parameters": same_name.parameters}}
    call = _call("a", "run_shell_command", {"script": "x"})

    [kept] = wire_messages(dialect, [_assistant(call)], [foreign])
    [ported] = wire_messages(dialect, [_assistant(_call("b", "run_shell_command", {"args": "ls"}))], [ours])

    assert kept.tool_calls == [call]
    assert ported.tool_calls is not None
    assert json.loads(ported.tool_calls[0]["function"]["arguments"]) == {"cmd": "ls"}


def test_tool_results_carry_the_wire_name() -> None:
    """Results answer the call under the name the request presents, which providers like Gemini require."""
    result = Message(role="tool", tool_call_id="a", tool_name="run_shell_command", content="done")

    [rendered] = wire_messages(_TOY, [result], _PRESENTED)

    assert rendered.tool_name == "Run"
    assert result.tool_name == "run_shell_command"
