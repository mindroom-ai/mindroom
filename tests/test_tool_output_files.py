"""Tests for central tool output redirection."""
# ruff: noqa: D103, S108

from __future__ import annotations

import functools
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from unittest.mock import patch

import pytest
from agno.media import Image
from agno.tools import Toolkit
from agno.tools.function import Function, ToolResult
from pydantic import BaseModel
from structlog.testing import capture_logs

from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.hooks import (
    EVENT_TOOL_AFTER_CALL,
    EVENT_TOOL_BEFORE_CALL,
    HookRegistry,
    ToolAfterCallContext,
    ToolBeforeCallContext,
    hook,
)
from mindroom.tool_system.agno_toolkits import adapt_toolkit, configure_toolkit_functions
from mindroom.tool_system.events import format_tool_combined
from mindroom.tool_system.invocation import ToolCallContext, run_tool_call
from mindroom.tool_system.output_files import (
    DEFAULT_TOOL_OUTPUT_AUTO_SAVE_THRESHOLD_BYTES,
    OUTPUT_PATH_ARGUMENT,
    ToolOutputFilePolicy,
    saved_tool_output_receipt,
    validate_output_path_syntax,
    with_optional_output_path,
    write_bytes_to_output_path,
)
from mindroom.tool_system.tool_function import ToolOutcome
from tests.agent_tool_helpers import detached_policy

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from mindroom.tool_system.tool_function import ToolFunction


def _policy(tmp_path: Path, *, max_bytes: int = 1024 * 1024) -> ToolOutputFilePolicy:
    return ToolOutputFilePolicy(workspace_root=tmp_path, max_bytes=max_bytes)


def _first_function(toolkit: Toolkit) -> ToolFunction:
    """Return the toolkit's first function as an agent's run offers it."""
    return adapt_toolkit(toolkit).functions[0]


def _with_output_files(function: Function, policy: ToolOutputFilePolicy | None) -> ToolFunction:
    """Return ``function``, registered alone in a toolkit offering output files by ``policy``, as a run offers it."""
    toolkit = Toolkit(name="functions")
    toolkit.functions[function.name] = function
    return _first_function(configure_toolkit_functions(toolkit, output_files=policy))


@dataclass(frozen=True)
class _Executed:
    """How one call ended: its result, or the error it raised."""

    status: Literal["success", "failure"]
    result: object


def _execute(function: ToolFunction, arguments: dict[str, object]) -> _Executed:
    """Call ``function`` with ``arguments`` as the model sent them."""
    try:
        return _Executed("success", function.entrypoint(**arguments))
    except Exception as error:
        return _Executed("failure", error)


def _assert_output_path_schema_is_optional(parameters: Mapping[str, Any]) -> None:
    output_schema = parameters["properties"][OUTPUT_PATH_ARGUMENT]
    assert output_schema["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert output_schema["default"] is None
    assert output_schema["description"].startswith("Optional")
    assert "workspace-relative path" in output_schema["description"]
    assert OUTPUT_PATH_ARGUMENT not in parameters["required"]


def test_runtime_policy_defaults_to_50_kib_auto_save_threshold(tmp_path: Path) -> None:
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})

    policy = ToolOutputFilePolicy.from_runtime(tmp_path, runtime_paths)

    assert DEFAULT_TOOL_OUTPUT_AUTO_SAVE_THRESHOLD_BYTES == 50 * 1024
    assert policy.auto_save_threshold_bytes == DEFAULT_TOOL_OUTPUT_AUTO_SAVE_THRESHOLD_BYTES


@pytest.mark.parametrize(
    ("process_env", "env_file", "expected"),
    [
        ({}, "MINDROOM_TOOL_OUTPUT_REDIRECT_MAX_BYTES=5\n", 5),
        ({"MINDROOM_TOOL_OUTPUT_REDIRECT_MAX_BYTES": "7"}, "MINDROOM_TOOL_OUTPUT_REDIRECT_MAX_BYTES=5\n", 7),
        ({"MINDROOM_TOOL_OUTPUT_REDIRECT_MAX_BYTES": ""}, "MINDROOM_TOOL_OUTPUT_REDIRECT_MAX_BYTES=5\n", 5),
        ({}, "", 64 * 1024**2),
    ],
    ids=["env_file", "process_env_wins", "blank_export_keeps_env_file", "live_environ_ignored"],
)
def test_runtime_policy_reads_the_output_cap_from_the_runtime_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    process_env: dict[str, str],
    env_file: str,
    expected: int,
) -> None:
    """The cap resolves like every runtime setting: the runtime's process env, then its `.env`, then the default."""
    monkeypatch.setenv("MINDROOM_TOOL_OUTPUT_REDIRECT_MAX_BYTES", "11")
    (tmp_path / ".env").write_text(env_file, encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env=process_env,
    )

    assert ToolOutputFilePolicy.from_runtime(tmp_path, runtime_paths).max_bytes == expected


def _receipt(result: object) -> dict[str, object]:
    assert isinstance(result, dict)
    envelope = result.get("mindroom_tool_output")
    assert isinstance(envelope, dict)
    return envelope


def test_an_optional_output_path_preserves_a_custom_schema_shape() -> None:
    parameters = with_optional_output_path(
        {
            "type": "object",
            "properties": {"attachment_id": {"type": "string", "description": "Context attachment."}},
            "required": ["attachment_id", OUTPUT_PATH_ARGUMENT],
            "additionalProperties": False,
        },
    )

    assert parameters["additionalProperties"] is False
    assert parameters["properties"]["attachment_id"] == {
        "type": "string",
        "description": "Context attachment.",
    }
    _assert_output_path_schema_is_optional(parameters)


def test_saved_tool_output_receipt_keeps_canonical_key_order() -> None:
    receipt = saved_tool_output_receipt(
        path="notes/result.txt",
        byte_count=5,
        output_format="binary",
        overwritten=False,
        sha256="abc123",
    )

    assert list(receipt) == ["status", "path", "bytes", "format", "overwritten", "sha256"]
    assert receipt == {
        "status": "saved_to_file",
        "path": "notes/result.txt",
        "bytes": 5,
        "format": "binary",
        "overwritten": False,
        "sha256": "abc123",
    }


def _plugin(*callbacks: object) -> object:
    return type(
        "Plugin",
        (),
        {
            "name": "test-plugin",
            "entry_config": PluginEntryConfig(path="test-plugin"),
            "plugin_order": 0,
            "discovered_hooks": tuple(callbacks),
            "discovered_automations": (),
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


def test_wrapped_entrypoint_drops_mindroom_output_path_before_calling_original(tmp_path: Path) -> None:
    seen: list[object] = []
    toolkit = _EchoToolkit(seen, result="large marker")
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {"text": "hi", OUTPUT_PATH_ARGUMENT: "tool-results/out.txt"})

    assert result.status == "success"
    assert seen == ["hi"]
    assert (tmp_path / "tool-results/out.txt").read_text(encoding="utf-8") == "large marker"
    assert _receipt(result.result)["status"] == "saved_to_file"


class _LineToolkit(Toolkit):
    def __init__(self, seen: list[object]) -> None:
        self.seen = seen
        super().__init__(name="lines", tools=[self.read_line])

    def read_line(self, start_line: int) -> str:
        self.seen.append(start_line)
        return "line"


def test_outer_functools_wraps_keeps_wrapped_entrypoint_validation(tmp_path: Path) -> None:
    """An outer wrapper, such as the sandbox proxy, wraps the finished entrypoint, which still coerces "28"."""
    seen: list[object] = []

    def to_worker(_name: str, inner: Callable[..., object]) -> Callable[..., object]:
        @functools.wraps(inner)
        def outer(*args: object, **kwargs: object) -> object:
            return inner(*args, **kwargs)

        return outer

    toolkit = configure_toolkit_functions(_LineToolkit(seen), output_files=_policy(tmp_path), to_worker=to_worker)
    result = _execute(_first_function(toolkit), {"start_line": "28"})

    assert result.status == "success"
    assert seen == [28]


@pytest.mark.parametrize("start_line", [28, "x"], ids=["valid-argument", "invalid-argument"])
def test_an_invalid_output_path_is_reported_before_the_arguments_are_validated(
    tmp_path: Path,
    start_line: object,
) -> None:
    """A non-string path answers with the output error, even beside an argument that fails validation; nothing runs."""
    seen: list[object] = []
    toolkit = configure_toolkit_functions(_LineToolkit(seen), output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {"start_line": start_line, OUTPUT_PATH_ARGUMENT: 5})

    assert _receipt(result.result) == {
        "status": "error",
        "error": "mindroom_output_path must be a workspace-relative string path.",
    }
    assert seen == []


def test_schema_adds_optional_output_path_for_normal_function(tmp_path: Path) -> None:
    toolkit = _EchoToolkit()
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    function = _first_function(toolkit)

    assert list(function.parameters["properties"]) == ["text", OUTPUT_PATH_ARGUMENT]
    assert function.parameters["required"] == ["text"]
    _assert_output_path_schema_is_optional(function.parameters)


def test_wrapped_function_keeps_its_documented_parameter_descriptions(tmp_path: Path) -> None:
    """The output path joins a function's documented arguments without displacing their descriptions."""

    def greet(name: str, greeting: str = "Hello") -> str:
        """Greet someone.

        Args:
            name: Who to greet.
            greeting: The word to greet them with.

        Returns:
            The greeting.

        """
        return f"{greeting} {name}"

    adapted = _with_output_files(Function(name="greet", entrypoint=greet), _policy(tmp_path))

    assert adapted.description == "Greet someone."
    assert adapted.parameters["properties"]["name"]["description"] == "Who to greet."
    assert adapted.parameters["properties"]["greeting"]["description"] == "The word to greet them with."
    _assert_output_path_schema_is_optional(adapted.parameters)


def test_a_strict_function_keeps_its_strict_schema_and_takes_no_output_path(tmp_path: Path) -> None:
    """A strict schema would require the output path, so a strict function keeps its author's contract instead."""
    toolkit = _EchoToolkit()
    toolkit.functions["echo"].strict = True
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    function = _first_function(toolkit)

    assert function.strict is True
    assert function.definition().strict is True
    assert list(function.parameters["properties"]) == ["text"]
    assert function.parameters["required"] == ["text"]
    assert _execute(function, {"text": "hi"}).result == "RAW"


def test_output_path_joins_the_schema_of_a_function_with_a_keyword_catch_all(tmp_path: Path) -> None:
    """The argument follows the function's named parameters, and ``**kwargs`` names no property of its own."""

    def tagged(name: str, **kwargs: object) -> str:
        return f"{name}{kwargs}"

    adapted = _with_output_files(Function(name="tagged", entrypoint=tagged), _policy(tmp_path))

    assert list(adapted.parameters["properties"]) == ["name", OUTPUT_PATH_ARGUMENT]
    _assert_output_path_schema_is_optional(adapted.parameters)


def test_a_function_taking_the_output_path_itself_keeps_its_own_while_its_siblings_offer_it(tmp_path: Path) -> None:
    def own(text: str, mindroom_output_path: str = "own") -> str:
        return f"{text}:{mindroom_output_path}"

    toolkit = _EchoToolkit()
    toolkit.register(own)
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    echo, own_function = adapt_toolkit(toolkit).functions

    _assert_output_path_schema_is_optional(echo.parameters)
    assert own_function.parameters["properties"][OUTPUT_PATH_ARGUMENT] == {"type": "string"}
    assert _execute(own_function, {"text": "hi", OUTPUT_PATH_ARGUMENT: "kept"}).result == "hi:kept"


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
    adapted = _with_output_files(function, _policy(tmp_path))

    assert adapted.parameters["additionalProperties"] is False
    _assert_output_path_schema_is_optional(adapted.parameters)


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
    adapted = _with_output_files(function, _policy(tmp_path))

    result = _execute(adapted, {"text": "saved", OUTPUT_PATH_ARGUMENT: "out.txt"})

    assert _receipt(result.result)["status"] == "saved_to_file"
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "saved"
    _assert_output_path_schema_is_optional(adapted.parameters)


def test_a_strict_skip_entrypoint_processing_function_keeps_its_schema_without_an_output_path(tmp_path: Path) -> None:
    def decorated_style_tool(**kwargs: object) -> object:
        return kwargs["text"]

    function = Function(
        name="decorated_style_tool",
        entrypoint=decorated_style_tool,
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        skip_entrypoint_processing=True,
        strict=True,
    )
    adapted = _with_output_files(function, _policy(tmp_path))

    assert adapted.strict is True
    assert adapted.parameters["properties"] == {"text": {"type": "string"}}
    assert adapted.parameters["required"] == ["text"]
    assert adapted.entrypoint is decorated_style_tool


def test_omitted_output_path_returns_original_result_unchanged(tmp_path: Path) -> None:
    raw_result = {"raw": "value"}
    toolkit = _EchoToolkit(result=raw_result)
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {"text": "hi"})

    assert result.result is raw_result
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("raw_output_path", ["", "   \t\n"])
def test_empty_output_path_returns_original_result_unchanged(tmp_path: Path, raw_output_path: str) -> None:
    raw_result = {"raw": "value"}
    toolkit = _EchoToolkit(result=raw_result)
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {"text": "hi", OUTPUT_PATH_ARGUMENT: raw_output_path})

    assert result.result is raw_result
    assert not list(tmp_path.iterdir())


def test_no_workspace_root_leaves_schema_unmodified() -> None:
    toolkit = _EchoToolkit()
    configure_toolkit_functions(toolkit, output_files=None)

    function = _first_function(toolkit)

    assert OUTPUT_PATH_ARGUMENT not in function.parameters["properties"]


def test_function_with_existing_mindroom_output_path_is_skipped_with_debug(tmp_path: Path) -> None:
    def existing(mindroom_output_path: str) -> str:
        return mindroom_output_path

    with capture_logs() as logs:
        function = _with_output_files(Function(name="existing", entrypoint=existing), _policy(tmp_path))

    assert logs == [
        {
            "argument_name": OUTPUT_PATH_ARGUMENT,
            "event": "tool_output_path_argument_collision",
            "function_name": "existing",
            "log_level": "debug",
        },
    ]
    result = _execute(function, {OUTPUT_PATH_ARGUMENT: "not-redirected"})
    assert result.result == "not-redirected"


def test_a_function_whose_authored_schema_takes_the_output_path_keeps_its_own(tmp_path: Path) -> None:
    """An authored schema that names ``mindroom_output_path`` keeps it, and the call hands it to the function."""

    def passthrough(**kwargs: object) -> object:
        return kwargs

    schema = {
        "type": "object",
        "properties": {OUTPUT_PATH_ARGUMENT: {"type": "string", "description": "Where the tool itself writes."}},
        "required": [],
    }
    adapted = _with_output_files(
        Function(name="passthrough", entrypoint=passthrough, parameters=schema, skip_entrypoint_processing=True),
        _policy(tmp_path),
    )

    assert adapted.parameters == schema
    assert _execute(adapted, {OUTPUT_PATH_ARGUMENT: "own.txt"}).result == {OUTPUT_PATH_ARGUMENT: "own.txt"}
    assert not list(tmp_path.iterdir())


def test_a_function_whose_entrypoint_takes_the_output_path_keeps_its_authored_schema(tmp_path: Path) -> None:
    """An entrypoint that takes ``mindroom_output_path`` handles it, even when its authored schema does not name it."""

    def own(text: str, mindroom_output_path: str | None = None) -> str:
        return f"{text}:{mindroom_output_path}"

    schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    adapted = _with_output_files(
        Function(name="own", entrypoint=own, parameters=schema, skip_entrypoint_processing=True),
        _policy(tmp_path),
    )

    assert adapted.parameters == schema
    assert _execute(adapted, {"text": "hi", OUTPUT_PATH_ARGUMENT: "kept"}).result == "hi:kept"
    assert not list(tmp_path.iterdir())


def test_an_authored_schema_takes_the_output_path_last_even_beside_a_keyword_catch_all(tmp_path: Path) -> None:
    """Only a derived schema places the path ahead of ``**kwargs``; an authored one, naming it too, gets it last."""

    def tagged(name: str, **kwargs: object) -> str:
        return f"{name}{kwargs}"

    adapted = _with_output_files(
        Function(
            name="tagged",
            entrypoint=tagged,
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}, "kwargs": {"type": "object"}},
                "required": ["name"],
            },
        ),
        _policy(tmp_path),
    )

    assert list(adapted.parameters["properties"]) == ["name", "kwargs", OUTPUT_PATH_ARGUMENT]
    _assert_output_path_schema_is_optional(adapted.parameters)


def test_text_result_written_as_utf8_and_receipt_returned(tmp_path: Path) -> None:
    toolkit = _EchoToolkit(result="hello \u2603")
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {"text": "hi", OUTPUT_PATH_ARGUMENT: "notes/result.txt"})

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
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {"text": "hi", OUTPUT_PATH_ARGUMENT: "result.json"})

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
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {"text": "hi", OUTPUT_PATH_ARGUMENT: "result.json"})

    assert _receipt(result.result)["format"] == "json"
    assert '"labels": [\n      "a",\n      "b"\n    ]' in (tmp_path / "result.json").read_text(encoding="utf-8")


def test_text_only_toolresult_written_as_text(tmp_path: Path) -> None:
    toolkit = _EchoToolkit(result=ToolResult(content="mcp text"))
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {"text": "hi", OUTPUT_PATH_ARGUMENT: "mcp.txt"})

    assert _receipt(result.result)["format"] == "text"
    assert (tmp_path / "mcp.txt").read_text(encoding="utf-8") == "mcp text"


def test_media_toolresult_is_rejected_without_writing(tmp_path: Path) -> None:
    toolkit = _EchoToolkit(result=ToolResult(content="hidden", images=[Image(url="https://example.test/image.png")]))
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {"text": "hi", OUTPUT_PATH_ARGUMENT: "media.txt"})

    assert _receipt(result.result) == {
        "status": "error",
        "error": "Tool media results cannot be redirected to workspace files.",
    }
    assert not (tmp_path / "media.txt").exists()


@pytest.mark.parametrize("raw_result", [b"binary", bytearray(b"binary")])
def test_bytes_result_is_rejected_without_writing(tmp_path: Path, raw_result: object) -> None:
    toolkit = _EchoToolkit(result=raw_result)
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {"text": "hi", OUTPUT_PATH_ARGUMENT: "binary.txt"})

    assert _receipt(result.result) == {
        "status": "error",
        "error": "Binary tool outputs cannot be redirected to workspace files.",
    }
    assert not (tmp_path / "binary.txt").exists()


def test_generator_or_stream_result_is_rejected_without_writing(tmp_path: Path) -> None:
    def stream() -> Iterator[str]:
        yield "chunk"

    toolkit = _EchoToolkit(result=stream())
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {"text": "hi", OUTPUT_PATH_ARGUMENT: "stream.txt"})

    assert _receipt(result.result) == {
        "status": "error",
        "error": "Generator or stream tool outputs cannot be redirected to workspace files.",
    }
    assert not (tmp_path / "stream.txt").exists()


def test_size_cap_exceeded_rejects_and_writes_nothing(tmp_path: Path) -> None:
    toolkit = _EchoToolkit(result="too large")
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path, max_bytes=3))

    result = _execute(_first_function(toolkit), {"text": "hi", OUTPUT_PATH_ARGUMENT: "large.txt"})

    receipt = _receipt(result.result)
    assert receipt["status"] == "error"
    assert "exceeds" in str(receipt["error"])
    assert not (tmp_path / "large.txt").exists()


def test_existing_regular_file_is_overwritten_atomically_and_receipt_marks_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "result.txt"
    target.write_text("old", encoding="utf-8")
    toolkit = _EchoToolkit(result="new")
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {"text": "hi", OUTPUT_PATH_ARGUMENT: "result.txt"})

    assert target.read_text(encoding="utf-8") == "new"
    assert _receipt(result.result)["overwritten"] is True


@pytest.mark.parametrize("failure", [None, "fsync", "replace"])
def test_output_publication_and_cleanup_stay_in_opened_directory(
    tmp_path: Path,
    failure: str | None,
) -> None:
    parent = tmp_path / "reports"
    parent.mkdir()
    (parent / "result.bin").write_bytes(b"old")
    original_parent = tmp_path / "renamed-reports"
    real_fsync = os.fsync

    def rename_parent_after_write(descriptor: int) -> None:
        real_fsync(descriptor)
        if original_parent.exists():
            return
        parent.rename(original_parent)
        parent.mkdir()
        (parent / "result.bin").write_bytes(b"replacement directory")
        if failure == "fsync":
            msg = "flush failed"
            raise OSError(msg)

    with (
        patch("os.fsync", side_effect=rename_parent_after_write),
        patch("os.replace", side_effect=OSError("replace failed") if failure == "replace" else os.replace),
    ):
        result = write_bytes_to_output_path(_policy(tmp_path), "reports/result.bin", b"\x00new\xff")

    assert (parent / "result.bin").read_bytes() == b"replacement directory"
    assert sorted(path.name for path in original_parent.iterdir()) == ["result.bin"]
    assert sorted(path.name for path in parent.iterdir()) == ["result.bin"]
    if failure is None:
        assert not isinstance(result, str)
        assert (original_parent / "result.bin").read_bytes() == b"\x00new\xff"
    else:
        assert isinstance(result, str)
        assert "Failed to write" in result
        assert (original_parent / "result.bin").read_bytes() == b"old"


@pytest.mark.parametrize("file_mode", [None, 0o600, 0o640])
def test_binary_output_replacement_preserves_requested_permissions(tmp_path: Path, file_mode: int | None) -> None:
    destination = tmp_path / "report.bin"
    destination.write_bytes(b"old")
    destination.chmod(0o666)

    result = write_bytes_to_output_path(_policy(tmp_path), "report.bin", b"\x00new\xff", file_mode=file_mode)

    assert not isinstance(result, str)
    assert result.overwritten
    assert destination.read_bytes() == b"\x00new\xff"
    assert stat.S_IMODE(destination.stat().st_mode) == (0o600 if file_mode is None else file_mode)
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("name_length", [230, 255])
def test_output_temp_name_does_not_limit_destination_basename(tmp_path: Path, name_length: int) -> None:
    filename = "x" * name_length

    result = write_bytes_to_output_path(_policy(tmp_path), filename, b"payload")

    assert not isinstance(result, str)
    assert (tmp_path / filename).read_bytes() == b"payload"
    assert list(tmp_path.iterdir()) == [tmp_path / filename]


@pytest.mark.parametrize("workspace_state", ["real", "home-relative", "replaced-by-link"])
def test_output_write_opens_the_workspace_as_spelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace_state: str,
) -> None:
    """Output lands in the workspace as spelled; a workspace replaced by a link is refused, not followed."""
    monkeypatch.setenv("HOME", str(tmp_path))
    workspace = tmp_path / "agents" / "code" / "workspace"
    workspace.mkdir(parents=True)
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    spelled = Path("~/agents/code/workspace") if workspace_state == "home-relative" else workspace
    policy = _policy(spelled)
    if workspace_state == "replaced-by-link":
        workspace.rename(tmp_path / "moved-workspace")
        workspace.symlink_to(credentials, target_is_directory=True)

    result = write_bytes_to_output_path(policy, "reports/nested/out.bin", b"payload")

    if workspace_state == "replaced-by-link":
        assert result == "Failed to write redirected tool output."
        assert list(credentials.iterdir()) == []
    else:
        assert not isinstance(result, str)
        assert (workspace / "reports" / "nested" / "out.bin").read_bytes() == b"payload"


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
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {"text": "hi", OUTPUT_PATH_ARGUMENT: bad_path})

    assert seen == []
    assert _receipt(result.result)["status"] == "error"


@pytest.mark.parametrize(
    "rejected_path",
    [
        "~/ride-trace.json",
        "$HOME/ride-trace.json",
        "%USERPROFILE%\\out.json",
        "reports/100%/out.json",
        "price$5/output.json",
        "~literal/output.json",
    ],
)
def test_expansion_characters_rejected_with_actionable_workspace_relative_message(rejected_path: str) -> None:
    error = validate_output_path_syntax(rejected_path)

    assert error is not None
    assert repr(rejected_path) in error
    assert "relative to the agent workspace" in error
    assert "must not start with `~` or contain `$` or `%`" in error
    assert "workspace-relative path" in error


def test_existing_directory_rejected_without_calling_tool(tmp_path: Path) -> None:
    (tmp_path / "existing").mkdir()
    seen: list[object] = []
    toolkit = _EchoToolkit(seen, result="should not run")
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {"text": "hi", OUTPUT_PATH_ARGUMENT: "existing"})

    assert seen == []
    assert _receipt(result.result)["status"] == "error"


@pytest.mark.parametrize("git_path", ["knowledge/docs/.git/config", "knowledge/docs/.git", "repo/.GIT/config"])
def test_git_metadata_output_paths_rejected_without_calling_tool(tmp_path: Path, git_path: str) -> None:
    """Redirected output must not author Git metadata that MindRoom later runs Git against."""
    git_config = tmp_path / "knowledge" / "docs" / ".git" / "config"
    git_config.parent.mkdir(parents=True)
    git_config.write_text("[core]\n", encoding="utf-8")
    seen: list[object] = []
    toolkit = _EchoToolkit(seen, result="should not run")
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {"text": "hi", OUTPUT_PATH_ARGUMENT: git_path})

    assert seen == []
    assert _receipt(result.result)["status"] == "error"
    assert validate_output_path_syntax(git_path) == "mindroom_output_path must not target Git metadata ('.git')."
    assert git_config.read_text(encoding="utf-8") == "[core]\n"
    assert not (tmp_path / "repo").exists()


def test_symlink_into_git_metadata_rejected_without_calling_tool(tmp_path: Path) -> None:
    """A workspace-internal link to ``.git`` must not bypass the name check."""
    git_config = tmp_path / "knowledge" / "docs" / ".git" / "config"
    git_config.parent.mkdir(parents=True)
    git_config.write_text("[core]\n", encoding="utf-8")
    (tmp_path / "linked").symlink_to(git_config.parent, target_is_directory=True)
    seen: list[object] = []
    toolkit = _EchoToolkit(seen, result="should not run")
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {"text": "hi", OUTPUT_PATH_ARGUMENT: "linked/config"})

    assert seen == []
    assert _receipt(result.result)["status"] == "error"
    assert git_config.read_text(encoding="utf-8") == "[core]\n"


def test_intermediate_symlink_escape_rejected_without_calling_tool(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    seen: list[object] = []
    toolkit = _EchoToolkit(seen, result="should not run")
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {"text": "hi", OUTPUT_PATH_ARGUMENT: "link/out.txt"})

    assert seen == []
    assert _receipt(result.result)["status"] == "error"
    assert not (outside / "out.txt").exists()


def test_existing_symlink_leaf_rejected_without_calling_tool(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-file"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "leaf").symlink_to(outside)
    seen: list[object] = []
    toolkit = _EchoToolkit(seen, result="should not run")
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {"text": "hi", OUTPUT_PATH_ARGUMENT: "leaf"})

    assert seen == []
    assert _receipt(result.result)["status"] == "error"
    assert outside.read_text(encoding="utf-8") == "outside"


def test_existing_regular_file_parent_rejected_without_calling_tool(tmp_path: Path) -> None:
    (tmp_path / "parentfile").write_text("not a directory", encoding="utf-8")
    seen: list[object] = []
    toolkit = _EchoToolkit(seen, result="should not run")
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {"text": "hi", OUTPUT_PATH_ARGUMENT: "parentfile/out.txt"})

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
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {OUTPUT_PATH_ARGUMENT: "link/nested/out.txt"})

    assert _receipt(result.result)["status"] == "error"
    assert not (outside / "nested").exists()
    assert str(outside) not in str(result.result)


def test_parent_rechecked_inside_workspace_before_atomic_replace(tmp_path: Path) -> None:
    toolkit = _EchoToolkit(result="payload")
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    with patch("mindroom.atomic_file.os.replace", side_effect=OSError("replace denied")):
        result = _execute(_first_function(toolkit), {"text": "hi", OUTPUT_PATH_ARGUMENT: "result.txt"})

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
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {OUTPUT_PATH_ARGUMENT: "result.txt"})

    assert result.status == "failure"
    assert "boom" in str(result.result)
    assert not (tmp_path / "result.txt").exists()


def test_processed_agno_function_returns_receipt_not_raw_result(tmp_path: Path) -> None:
    marker = "ISSUE200_RAW_MARKER"
    toolkit = _EchoToolkit(result=marker)
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))
    function = _first_function(toolkit)

    result = _execute(function, {"text": "hi", OUTPUT_PATH_ARGUMENT: "result.txt"})

    assert marker in (tmp_path / "result.txt").read_text(encoding="utf-8")
    assert marker not in str(result.result)
    assert _receipt(result.result)["status"] == "saved_to_file"


def test_tool_trace_contains_path_and_receipt_but_not_raw_large_marker(tmp_path: Path) -> None:
    marker = "ISSUE200_TRACE_MARKER"
    toolkit = _EchoToolkit(result=marker * 20)
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {"text": "hi", OUTPUT_PATH_ARGUMENT: "trace/result.txt"})
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
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {"text": "hi", OUTPUT_PATH_ARGUMENT: "mcp.txt"})

    assert marker in (tmp_path / "mcp.txt").read_text(encoding="utf-8")
    assert marker not in str(result.result)


def test_existing_tool_behavior_unchanged_without_output_path(tmp_path: Path) -> None:
    seen: list[object] = []
    toolkit = _EchoToolkit(seen, result="RAW")
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))

    result = _execute(_first_function(toolkit), {"text": "hi"})

    assert seen == ["hi"]
    assert result.result == "RAW"


def test_large_text_result_auto_saved_without_output_path(tmp_path: Path) -> None:
    marker = "ISSUE200_AUTO_MARKER"
    raw_output = marker * 1_000
    toolkit = _EchoToolkit(result=raw_output)
    policy = ToolOutputFilePolicy(workspace_root=tmp_path, max_bytes=100_000, auto_save_threshold_bytes=100)
    configure_toolkit_functions(toolkit, output_files=policy)

    result = _execute(_first_function(toolkit), {"text": "hi"})

    receipt = _receipt(result.result)
    assert receipt["status"] == "saved_to_file"
    assert receipt["auto_saved"] is True
    assert receipt["threshold_bytes"] == 100
    assert receipt["format"] == "text"
    assert marker in str(receipt["preview"])
    assert "line range" in str(receipt["hint"])
    assert raw_output not in str(result.result)
    saved_path = tmp_path / cast("str", receipt["path"])
    assert raw_output == saved_path.read_text(encoding="utf-8")


def test_large_json_result_auto_saved_without_output_path(tmp_path: Path) -> None:
    policy = ToolOutputFilePolicy(workspace_root=tmp_path, max_bytes=10_000, auto_save_threshold_bytes=40)
    toolkit = _EchoToolkit(result={"items": ["z" * 30, "a" * 30]})
    configure_toolkit_functions(toolkit, output_files=policy)

    result = _execute(_first_function(toolkit), {"text": "hi"})

    receipt = _receipt(result.result)
    assert receipt["format"] == "json"
    assert str(receipt["path"]).endswith(".json")
    saved_text = (tmp_path / cast("str", receipt["path"])).read_text(encoding="utf-8")
    assert '"items"' in saved_text
    assert "z" * 30 in saved_text


def test_explicit_output_path_takes_precedence_over_auto_save_threshold(tmp_path: Path) -> None:
    policy = ToolOutputFilePolicy(workspace_root=tmp_path, max_bytes=10_000, auto_save_threshold_bytes=10)
    toolkit = _EchoToolkit(result="x" * 200)
    configure_toolkit_functions(toolkit, output_files=policy)

    result = _execute(_first_function(toolkit), {"text": "hi", OUTPUT_PATH_ARGUMENT: "explicit.txt"})

    receipt = _receipt(result.result)
    assert receipt["path"] == "explicit.txt"
    assert "auto_saved" not in receipt
    assert (tmp_path / "explicit.txt").read_text(encoding="utf-8") == "x" * 200


def test_large_result_over_auto_save_limit_returns_compact_error(tmp_path: Path) -> None:
    marker = "ISSUE200_TOO_LARGE"
    policy = ToolOutputFilePolicy(workspace_root=tmp_path, max_bytes=100, auto_save_threshold_bytes=10)
    toolkit = _EchoToolkit(result=marker * 20)
    configure_toolkit_functions(toolkit, output_files=policy)

    result = _execute(_first_function(toolkit), {"text": "hi"})

    receipt = _receipt(result.result)
    assert receipt["status"] == "error"
    assert "auto-save limit" in str(receipt["error"])
    assert marker not in str(result.result)
    assert not list(tmp_path.iterdir())


def test_auto_save_serialization_failure_preserves_original_result(tmp_path: Path) -> None:
    class Unstringable:
        def __str__(self) -> str:
            msg = "cannot stringify"
            raise RuntimeError(msg)

    raw_result = Unstringable()
    policy = ToolOutputFilePolicy(workspace_root=tmp_path, max_bytes=100, auto_save_threshold_bytes=10)
    toolkit = _EchoToolkit(result=raw_result)
    configure_toolkit_functions(toolkit, output_files=policy)

    result = _execute(_first_function(toolkit), {"text": "hi"})

    assert result.result is raw_result
    assert not list(tmp_path.iterdir())


def test_auto_save_write_failure_preserves_original_result(tmp_path: Path) -> None:
    raw_result = "x" * 200
    policy = ToolOutputFilePolicy(workspace_root=tmp_path, max_bytes=1_000, auto_save_threshold_bytes=10)
    toolkit = _EchoToolkit(result=raw_result)
    configure_toolkit_functions(toolkit, output_files=policy)

    with patch("mindroom.tool_system.output_files._write_auto_saved_result", side_effect=RuntimeError("boom")):
        result = _execute(_first_function(toolkit), {"text": "hi"})

    assert result.result == raw_result
    assert not list(tmp_path.iterdir())


async def _run_through_hooks(toolkit: Toolkit, registry: HookRegistry, arguments: dict[str, object]) -> object:
    """Run the toolkit's first function through the tool-call policy and return what the call returned."""
    result = await run_tool_call(
        _first_function(toolkit),
        arguments,
        ToolCallContext(run_id="run-1", session_id="session-1", call_id="call-1", user_id=None),
        middleware=(detached_policy(registry),),
    )
    return result.value


@pytest.mark.asyncio
async def test_before_hook_sees_mindroom_output_path_and_after_hook_receives_receipt(tmp_path: Path) -> None:
    marker = "ISSUE200_HOOK_MARKER"
    seen: list[tuple[str, object]] = []

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(ctx: ToolBeforeCallContext) -> None:
        seen.append(("before", dict(ctx.arguments)))

    @hook(EVENT_TOOL_AFTER_CALL)
    async def after(ctx: ToolAfterCallContext) -> None:
        seen.append(("after", ctx.result))

    toolkit = _EchoToolkit(result=marker)
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))
    registry = HookRegistry.from_plugins([_plugin(before, after)])

    result = await _run_through_hooks(toolkit, registry, {"text": "hi", OUTPUT_PATH_ARGUMENT: "hook.txt"})

    assert marker in (tmp_path / "hook.txt").read_text(encoding="utf-8")
    assert seen[0] == ("before", {"text": "hi", OUTPUT_PATH_ARGUMENT: "hook.txt"})
    assert seen[1][0] == "after"
    assert marker not in str(seen[1][1])
    assert seen[1][1] == result


@pytest.mark.asyncio
async def test_before_hook_decline_blocks_tool_execution_and_file_creation(tmp_path: Path) -> None:
    seen: list[object] = []

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(ctx: ToolBeforeCallContext) -> None:
        ctx.decline("blocked")

    @hook(EVENT_TOOL_AFTER_CALL)
    async def after(ctx: ToolAfterCallContext) -> None:
        seen.append(("after", ctx.blocked, ctx.result))

    toolkit = _EchoToolkit(seen, result="should not run")
    configure_toolkit_functions(toolkit, output_files=_policy(tmp_path))
    registry = HookRegistry.from_plugins([_plugin(before, after)])

    result = await _run_through_hooks(toolkit, registry, {"text": "hi", OUTPUT_PATH_ARGUMENT: "blocked.txt"})

    assert not (tmp_path / "blocked.txt").exists()
    assert isinstance(result, ToolOutcome)
    assert result.is_error
    assert "[TOOL CALL DECLINED]" in result.content
    assert seen == [("after", True, result.content)]
