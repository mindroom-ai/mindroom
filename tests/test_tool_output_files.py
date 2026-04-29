"""Tests for central tool output redirection."""
# ruff: noqa: D103, S108

from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest
from agno.media import Image
from agno.models.openai.chat import OpenAIChat
from agno.tools import Toolkit
from agno.tools.function import Function, FunctionCall, ToolResult
from pydantic import BaseModel

from mindroom.config.plugin import PluginEntryConfig
from mindroom.hooks import (
    EVENT_TOOL_AFTER_CALL,
    EVENT_TOOL_BEFORE_CALL,
    HookRegistry,
    ToolAfterCallContext,
    ToolBeforeCallContext,
    hook,
)
from mindroom.tool_system.events import format_tool_combined
from mindroom.tool_system.output_files import (
    OUTPUT_PATH_ARGUMENT,
    ToolOutputFilePolicy,
    wrap_function_for_output_files,
    wrap_toolkit_for_output_files,
)
from mindroom.tool_system.tool_hooks import build_tool_hook_bridge, prepend_tool_hook_bridge

if TYPE_CHECKING:
    from collections.abc import Iterator


def _policy(tmp_path: Path, *, max_bytes: int = 1024 * 1024) -> ToolOutputFilePolicy:
    return ToolOutputFilePolicy(workspace_root=tmp_path, max_bytes=max_bytes)


def _first_function(toolkit: Toolkit) -> Function:
    return next(iter(toolkit.functions.values()))


def _processed(function: Function) -> Function:
    copied = function.model_copy(deep=True)
    copied.process_entrypoint()
    return copied


def _processed_strict(function: Function) -> Function:
    copied = function.model_copy(deep=True)
    copied.process_entrypoint(strict=True)
    return copied


def _openai_tool_payload(function: Function, *, strict: bool) -> dict[str, object]:
    copied = function.model_copy(deep=True)
    effective_strict = strict if copied.strict is None else copied.strict
    copied.process_entrypoint(strict=effective_strict)
    formatted_tools = OpenAIChat(id="gpt-5.4", api_key="sk-test")._format_tools([copied])
    payload = formatted_tools[0]["function"]
    assert isinstance(payload, dict)
    return payload


def _assert_output_path_schema_is_optional(function: Function) -> None:
    output_schema = function.parameters["properties"][OUTPUT_PATH_ARGUMENT]
    assert output_schema["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert output_schema["default"] is None
    assert output_schema["description"].startswith("Optional")
    assert "workspace-relative path" in output_schema["description"]
    assert OUTPUT_PATH_ARGUMENT not in function.parameters["required"]


def _receipt(result: object) -> dict[str, object]:
    assert isinstance(result, dict)
    envelope = result.get("mindroom_tool_output")
    assert isinstance(envelope, dict)
    return envelope


def _plugin(*callbacks: object) -> object:
    return type(
        "Plugin",
        (),
        {
            "name": "test-plugin",
            "entry_config": PluginEntryConfig(path="test-plugin"),
            "plugin_order": 0,
            "discovered_hooks": tuple(callbacks),
        },
    )()


class _EchoToolkit(Toolkit):
    def __init__(self, seen: list[object] | None = None, result: object = "RAW") -> None:
        self.seen = seen if seen is not None else []
        self.result = result
        super().__init__(name="echo", tools=[self.echo])

    def echo(self, text: str) -> object:
        self.seen.append(text)
        return self.result


def test_wrapped_entrypoint_signature_exposes_mindroom_output_path(tmp_path: Path) -> None:
    toolkit = _EchoToolkit()
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    function = _first_function(toolkit)
    assert OUTPUT_PATH_ARGUMENT in inspect.signature(function.entrypoint).parameters


def test_wrapped_entrypoint_drops_mindroom_output_path_before_calling_original(tmp_path: Path) -> None:
    seen: list[object] = []
    toolkit = _EchoToolkit(seen, result="large marker")
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={"text": "hi", OUTPUT_PATH_ARGUMENT: "tool-results/out.txt"},
        call_id="call-1",
    ).execute()

    assert result.status == "success"
    assert seen == ["hi"]
    assert (tmp_path / "tool-results/out.txt").read_text(encoding="utf-8") == "large marker"
    assert _receipt(result.result)["status"] == "saved_to_file"


def test_schema_adds_optional_output_path_for_normal_function(tmp_path: Path) -> None:
    toolkit = _EchoToolkit()
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    function = _processed(_first_function(toolkit))

    _assert_output_path_schema_is_optional(function)


def test_schema_keeps_output_path_optional_after_strict_processing(tmp_path: Path) -> None:
    toolkit = _EchoToolkit()
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    function = _processed_strict(_first_function(toolkit))

    assert "text" in function.parameters["required"]
    _assert_output_path_schema_is_optional(function)


def test_openai_payload_opts_wrapped_tool_out_of_strict_mode(tmp_path: Path) -> None:
    toolkit = _EchoToolkit()
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    payload = _openai_tool_payload(_first_function(toolkit), strict=True)

    parameters = payload["parameters"]
    assert isinstance(parameters, dict)
    output_schema = parameters["properties"][OUTPUT_PATH_ARGUMENT]
    assert payload["strict"] is False
    assert output_schema["default"] is None
    assert output_schema["description"].startswith("Optional")
    assert OUTPUT_PATH_ARGUMENT not in parameters["required"]


def test_schema_handles_strict_or_additional_properties_false_function(tmp_path: Path) -> None:
    def strict_tool(text: str) -> str:
        return text

    function = Function(
        name="strict_tool",
        entrypoint=strict_tool,
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        skip_entrypoint_processing=True,
    )
    wrap_function_for_output_files(function, _policy(tmp_path))

    assert function.parameters["additionalProperties"] is False
    _assert_output_path_schema_is_optional(function)


def test_schema_handles_skip_entrypoint_processing_function(tmp_path: Path) -> None:
    def decorated_style_tool(**kwargs: object) -> object:
        return kwargs["text"]

    function = Function(
        name="decorated_style_tool",
        entrypoint=decorated_style_tool,
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        skip_entrypoint_processing=True,
    )
    wrap_function_for_output_files(function, _policy(tmp_path))

    result = FunctionCall(
        function=function,
        arguments={"text": "saved", OUTPUT_PATH_ARGUMENT: "out.txt"},
        call_id="call-1",
    ).execute()

    assert _receipt(result.result)["status"] == "saved_to_file"
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "saved"
    _assert_output_path_schema_is_optional(function)


def test_schema_handles_skip_entrypoint_processing_function_in_strict_mode(tmp_path: Path) -> None:
    def decorated_style_tool(**kwargs: object) -> object:
        return kwargs["text"]

    function = Function(
        name="decorated_style_tool",
        entrypoint=decorated_style_tool,
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        skip_entrypoint_processing=True,
    )
    wrap_function_for_output_files(function, _policy(tmp_path))

    function = _processed_strict(function)

    assert "text" in function.parameters["required"]
    _assert_output_path_schema_is_optional(function)


def test_model_copy_update_preserves_output_path_schema_postprocessor(tmp_path: Path) -> None:
    toolkit = _EchoToolkit()
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    copied = cast("Any", _first_function(toolkit).model_copy)(
        update={"description": "Updated description"},
        deep=True,
    )
    copied.process_entrypoint()

    assert copied.description == "Updated description"
    _assert_output_path_schema_is_optional(copied)


def test_omitted_output_path_returns_original_result_unchanged(tmp_path: Path) -> None:
    raw_result = {"raw": "value"}
    toolkit = _EchoToolkit(result=raw_result)
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={"text": "hi"},
        call_id="call-1",
    ).execute()

    assert result.result is raw_result
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("raw_output_path", ["", "   \t\n"])
def test_empty_output_path_returns_original_result_unchanged(tmp_path: Path, raw_output_path: str) -> None:
    raw_result = {"raw": "value"}
    toolkit = _EchoToolkit(result=raw_result)
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={"text": "hi", OUTPUT_PATH_ARGUMENT: raw_output_path},
        call_id="call-1",
    ).execute()

    assert result.result is raw_result
    assert not list(tmp_path.iterdir())


def test_no_workspace_root_leaves_schema_unmodified() -> None:
    toolkit = _EchoToolkit()
    wrap_toolkit_for_output_files(toolkit, None)

    function = _processed(_first_function(toolkit))

    assert OUTPUT_PATH_ARGUMENT not in inspect.signature(function.entrypoint).parameters
    assert OUTPUT_PATH_ARGUMENT not in function.parameters["properties"]


def test_function_with_existing_mindroom_output_path_is_skipped_with_warning(tmp_path: Path) -> None:
    def existing(mindroom_output_path: str) -> str:
        return mindroom_output_path

    function = Function(name="existing", entrypoint=existing)
    with patch("mindroom.tool_system.output_files.logger.warning") as warning:
        wrap_function_for_output_files(function, _policy(tmp_path))

    warning.assert_called_once()
    result = FunctionCall(
        function=function,
        arguments={OUTPUT_PATH_ARGUMENT: "not-redirected"},
        call_id="call-1",
    ).execute()
    assert result.result == "not-redirected"


def test_text_result_written_as_utf8_and_receipt_returned(tmp_path: Path) -> None:
    toolkit = _EchoToolkit(result="hello \u2603")
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={"text": "hi", OUTPUT_PATH_ARGUMENT: "notes/result.txt"},
        call_id="call-1",
    ).execute()

    assert (tmp_path / "notes/result.txt").read_text(encoding="utf-8") == "hello \u2603"
    receipt = _receipt(result.result)
    assert receipt == {
        "status": "saved_to_file",
        "path": "notes/result.txt",
        "bytes": len("hello \u2603".encode("utf-8")),
        "format": "text",
        "overwritten": False,
    }


def test_json_compatible_result_written_as_stable_json_with_trailing_newline(tmp_path: Path) -> None:
    toolkit = _EchoToolkit(result={"z": 1, "a": ["b"]})
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={"text": "hi", OUTPUT_PATH_ARGUMENT: "result.json"},
        call_id="call-1",
    ).execute()

    assert _receipt(result.result)["format"] == "json"
    assert (tmp_path / "result.json").read_text(encoding="utf-8") == '{\n  "a": [\n    "b"\n  ],\n  "z": 1\n}\n'


def test_dataclass_or_pydantic_result_normalized_to_json(tmp_path: Path) -> None:
    @dataclass(frozen=True)
    class DataclassResult:
        path: Path
        labels: set[str]

    class PydanticResult(BaseModel):
        label: str

    toolkit = _EchoToolkit(
        result={
            "model": PydanticResult(label="ok"),
            "payload": DataclassResult(Path("x.txt"), {"b", "a"}),
        },
    )
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={"text": "hi", OUTPUT_PATH_ARGUMENT: "result.json"},
        call_id="call-1",
    ).execute()

    assert _receipt(result.result)["format"] == "json"
    assert '"labels": [\n      "a",\n      "b"\n    ]' in (tmp_path / "result.json").read_text(encoding="utf-8")


def test_text_only_toolresult_written_as_text(tmp_path: Path) -> None:
    toolkit = _EchoToolkit(result=ToolResult(content="mcp text"))
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={"text": "hi", OUTPUT_PATH_ARGUMENT: "mcp.txt"},
        call_id="call-1",
    ).execute()

    assert _receipt(result.result)["format"] == "text"
    assert (tmp_path / "mcp.txt").read_text(encoding="utf-8") == "mcp text"


def test_media_toolresult_is_rejected_without_writing(tmp_path: Path) -> None:
    toolkit = _EchoToolkit(result=ToolResult(content="hidden", images=[Image(url="https://example.test/image.png")]))
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={"text": "hi", OUTPUT_PATH_ARGUMENT: "media.txt"},
        call_id="call-1",
    ).execute()

    assert _receipt(result.result)["status"] == "error"
    assert not (tmp_path / "media.txt").exists()


@pytest.mark.parametrize("raw_result", [b"binary", bytearray(b"binary")])
def test_bytes_result_is_rejected_without_writing(tmp_path: Path, raw_result: object) -> None:
    toolkit = _EchoToolkit(result=raw_result)
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={"text": "hi", OUTPUT_PATH_ARGUMENT: "binary.txt"},
        call_id="call-1",
    ).execute()

    assert _receipt(result.result)["status"] == "error"
    assert not (tmp_path / "binary.txt").exists()


def test_generator_or_stream_result_is_rejected_without_writing(tmp_path: Path) -> None:
    def stream() -> Iterator[str]:
        yield "chunk"

    toolkit = _EchoToolkit(result=stream())
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={"text": "hi", OUTPUT_PATH_ARGUMENT: "stream.txt"},
        call_id="call-1",
    ).execute()

    assert _receipt(result.result)["status"] == "error"
    assert not (tmp_path / "stream.txt").exists()


def test_size_cap_exceeded_rejects_and_writes_nothing(tmp_path: Path) -> None:
    toolkit = _EchoToolkit(result="too large")
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path, max_bytes=3))

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={"text": "hi", OUTPUT_PATH_ARGUMENT: "large.txt"},
        call_id="call-1",
    ).execute()

    receipt = _receipt(result.result)
    assert receipt["status"] == "error"
    assert "exceeds" in str(receipt["error"])
    assert not (tmp_path / "large.txt").exists()


def test_existing_regular_file_is_overwritten_atomically_and_receipt_marks_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "result.txt"
    target.write_text("old", encoding="utf-8")
    toolkit = _EchoToolkit(result="new")
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={"text": "hi", OUTPUT_PATH_ARGUMENT: "result.txt"},
        call_id="call-1",
    ).execute()

    assert target.read_text(encoding="utf-8") == "new"
    assert _receipt(result.result)["overwritten"] is True


@pytest.mark.parametrize(
    "bad_path",
    [
        "/tmp/out.txt",
        "../escape.txt",
        ".",
        "bad\x00path",
        "$HOME/out.txt",
        "~/out.txt",
    ],
)
def test_invalid_output_paths_rejected_without_calling_tool(tmp_path: Path, bad_path: str) -> None:
    seen: list[object] = []
    toolkit = _EchoToolkit(seen, result="should not run")
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={"text": "hi", OUTPUT_PATH_ARGUMENT: bad_path},
        call_id="call-1",
    ).execute()

    assert seen == []
    assert _receipt(result.result)["status"] == "error"


def test_existing_directory_rejected_without_calling_tool(tmp_path: Path) -> None:
    (tmp_path / "existing").mkdir()
    seen: list[object] = []
    toolkit = _EchoToolkit(seen, result="should not run")
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={"text": "hi", OUTPUT_PATH_ARGUMENT: "existing"},
        call_id="call-1",
    ).execute()

    assert seen == []
    assert _receipt(result.result)["status"] == "error"


def test_intermediate_symlink_escape_rejected_without_calling_tool(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    seen: list[object] = []
    toolkit = _EchoToolkit(seen, result="should not run")
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={"text": "hi", OUTPUT_PATH_ARGUMENT: "link/out.txt"},
        call_id="call-1",
    ).execute()

    assert seen == []
    assert _receipt(result.result)["status"] == "error"
    assert not (outside / "out.txt").exists()


def test_existing_symlink_leaf_rejected_without_calling_tool(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-file"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "leaf").symlink_to(outside)
    seen: list[object] = []
    toolkit = _EchoToolkit(seen, result="should not run")
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={"text": "hi", OUTPUT_PATH_ARGUMENT: "leaf"},
        call_id="call-1",
    ).execute()

    assert seen == []
    assert _receipt(result.result)["status"] == "error"
    assert outside.read_text(encoding="utf-8") == "outside"


def test_existing_regular_file_parent_rejected_without_calling_tool(tmp_path: Path) -> None:
    (tmp_path / "parentfile").write_text("not a directory", encoding="utf-8")
    seen: list[object] = []
    toolkit = _EchoToolkit(seen, result="should not run")
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={"text": "hi", OUTPUT_PATH_ARGUMENT: "parentfile/out.txt"},
        call_id="call-1",
    ).execute()

    assert seen == []
    assert _receipt(result.result)["status"] == "error"
    assert str(tmp_path) not in str(result.result)


def test_post_validation_symlink_parent_rejected_before_directory_creation(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-post-validation-outside"
    outside.mkdir()

    class MutatingToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="mutating", tools=[self.mutate])

        def mutate(self) -> str:
            (tmp_path / "link").symlink_to(outside, target_is_directory=True)
            return "payload"

    toolkit = MutatingToolkit()
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={OUTPUT_PATH_ARGUMENT: "link/nested/out.txt"},
        call_id="call-1",
    ).execute()

    assert _receipt(result.result)["status"] == "error"
    assert not (outside / "nested").exists()
    assert str(outside) not in str(result.result)


def test_parent_rechecked_inside_workspace_before_atomic_replace(tmp_path: Path) -> None:
    toolkit = _EchoToolkit(result="payload")
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    with patch("mindroom.tool_system.output_files.os.replace", side_effect=OSError("replace denied")):
        result = FunctionCall(
            function=_first_function(toolkit),
            arguments={"text": "hi", OUTPUT_PATH_ARGUMENT: "result.txt"},
            call_id="call-1",
        ).execute()

    assert _receipt(result.result)["status"] == "error"
    assert str(tmp_path) not in str(result.result)
    assert not (tmp_path / "result.txt").exists()


def test_tool_exception_propagates_and_no_file_written(tmp_path: Path) -> None:
    class FailingToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="failing", tools=[self.fail])

        def fail(self) -> str:
            msg = "boom"
            raise RuntimeError(msg)

    toolkit = FailingToolkit()
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={OUTPUT_PATH_ARGUMENT: "result.txt"},
        call_id="call-1",
    ).execute()

    assert result.status == "failure"
    assert "boom" in str(result.error)
    assert not (tmp_path / "result.txt").exists()


def test_processed_agno_function_returns_receipt_not_raw_result(tmp_path: Path) -> None:
    marker = "ISSUE200_RAW_MARKER"
    toolkit = _EchoToolkit(result=marker)
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))
    function = _processed(_first_function(toolkit))

    result = FunctionCall(
        function=function,
        arguments={"text": "hi", OUTPUT_PATH_ARGUMENT: "result.txt"},
        call_id="call-1",
    ).execute()

    assert marker in (tmp_path / "result.txt").read_text(encoding="utf-8")
    assert marker not in str(result.result)
    assert _receipt(result.result)["status"] == "saved_to_file"


def test_tool_trace_contains_path_and_receipt_but_not_raw_large_marker(tmp_path: Path) -> None:
    marker = "ISSUE200_TRACE_MARKER"
    toolkit = _EchoToolkit(result=marker * 20)
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={"text": "hi", OUTPUT_PATH_ARGUMENT: "trace/result.txt"},
        call_id="call-1",
    ).execute()
    _block, trace = format_tool_combined(
        "echo",
        {"text": "hi", OUTPUT_PATH_ARGUMENT: "trace/result.txt"},
        result.result,
    )

    assert marker in (tmp_path / "trace/result.txt").read_text(encoding="utf-8")
    assert trace.args_preview is not None
    assert "trace/result.txt" in trace.args_preview
    assert trace.result_preview is not None
    assert "saved_to_file" in trace.result_preview
    assert marker not in trace.result_preview


def test_mcp_style_tool_result_large_marker_saved_but_absent_from_returned_result(tmp_path: Path) -> None:
    marker = "ISSUE200_MCP_MARKER"
    toolkit = _EchoToolkit(result=ToolResult(content=marker * 20))
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={"text": "hi", OUTPUT_PATH_ARGUMENT: "mcp.txt"},
        call_id="call-1",
    ).execute()

    assert marker in (tmp_path / "mcp.txt").read_text(encoding="utf-8")
    assert marker not in str(result.result)


def test_existing_tool_behavior_unchanged_without_output_path(tmp_path: Path) -> None:
    seen: list[object] = []
    toolkit = _EchoToolkit(seen, result="RAW")
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={"text": "hi"},
        call_id="call-1",
    ).execute()

    assert seen == ["hi"]
    assert result.result == "RAW"


def test_before_hook_sees_mindroom_output_path_and_after_hook_receives_receipt(tmp_path: Path) -> None:
    marker = "ISSUE200_HOOK_MARKER"
    seen: list[tuple[str, object]] = []

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(ctx: ToolBeforeCallContext) -> None:
        seen.append(("before", dict(ctx.arguments)))

    @hook(EVENT_TOOL_AFTER_CALL)
    async def after(ctx: ToolAfterCallContext) -> None:
        seen.append(("after", ctx.result))

    toolkit = _EchoToolkit(result=marker)
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))
    bridge = build_tool_hook_bridge(HookRegistry.from_plugins([_plugin(before, after)]), agent_name="code")
    prepend_tool_hook_bridge(toolkit, bridge)

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={"text": "hi", OUTPUT_PATH_ARGUMENT: "hook.txt"},
        call_id="call-1",
    ).execute()

    assert marker in (tmp_path / "hook.txt").read_text(encoding="utf-8")
    assert seen[0] == ("before", {"text": "hi", OUTPUT_PATH_ARGUMENT: "hook.txt"})
    assert seen[1][0] == "after"
    assert marker not in str(seen[1][1])
    assert seen[1][1] == result.result


def test_before_hook_decline_blocks_tool_execution_and_file_creation(tmp_path: Path) -> None:
    seen: list[object] = []

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(ctx: ToolBeforeCallContext) -> None:
        ctx.decline("blocked")

    @hook(EVENT_TOOL_AFTER_CALL)
    async def after(ctx: ToolAfterCallContext) -> None:
        seen.append(("after", ctx.blocked, ctx.result))

    toolkit = _EchoToolkit(seen, result="should not run")
    wrap_toolkit_for_output_files(toolkit, _policy(tmp_path))
    bridge = build_tool_hook_bridge(HookRegistry.from_plugins([_plugin(before, after)]), agent_name="code")
    prepend_tool_hook_bridge(toolkit, bridge)

    result = FunctionCall(
        function=_first_function(toolkit),
        arguments={"text": "hi", OUTPUT_PATH_ARGUMENT: "blocked.txt"},
        call_id="call-1",
    ).execute()

    assert not (tmp_path / "blocked.txt").exists()
    assert seen == [("after", True, result.result)]
    assert "[TOOL CALL DECLINED]" in str(result.result)
