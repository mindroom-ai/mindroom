"""Central support for redirecting tool output into workspace files."""

from __future__ import annotations

import functools
import inspect
import json
import stat
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import asdict, dataclass, is_dataclass, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel

from mindroom.atomic_file import atomic_write_bytes_at
from mindroom.constants import DEFAULT_TOOL_OUTPUT_AUTO_SAVE_THRESHOLD_BYTES, DEFAULT_TOOL_OUTPUT_MAX_BYTES
from mindroom.logging_config import get_logger
from mindroom.path_confinement import is_git_metadata_path, open_directory_within_root
from mindroom.workspaces import resolve_relative_path_within_root_preserving_leaf

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.tool_function import ToolFunction

logger = get_logger(__name__)

OUTPUT_PATH_ARGUMENT = "mindroom_output_path"
_OUTPUT_PATH_ARGUMENT_DESCRIPTION = (
    "Optional MindRoom-managed workspace-relative path. "
    "If set, the full supported tool output is written to this file in your workspace and the tool returns only a "
    "compact receipt. "
    "Use this for large output you plan to inspect later with file, coding, python, or shell tools."
)
_MAX_BYTES_ENV = "MINDROOM_TOOL_OUTPUT_REDIRECT_MAX_BYTES"
_AUTO_SAVE_PREVIEW_BYTES = 8192
_AUTO_SAVE_ROOT = "mindroom_tool_outputs"
_AUTO_SAVE_READ_HINT = (
    "Do not read this whole file: it is over threshold_bytes, so the result would be saved to a file again. "
    "Read it in parts instead, such as a line range (for example with read_file_chunk) or a search for what you need."
)
_TEXT_FALLBACK_HEADER = (
    "MindRoom tool output serialized with text fallback because the result was not JSON-normalizable.\n\n"
)


@dataclass(frozen=True)
class ToolOutputFilePolicy:
    """Resolved policy for one toolkit's model-requested output files."""

    workspace_root: Path
    max_bytes: int = DEFAULT_TOOL_OUTPUT_MAX_BYTES
    auto_save_threshold_bytes: int = DEFAULT_TOOL_OUTPUT_AUTO_SAVE_THRESHOLD_BYTES

    @classmethod
    def from_runtime(
        cls,
        workspace_root: Path,
        runtime_paths: RuntimePaths,
        *,
        auto_save_threshold_bytes: int = DEFAULT_TOOL_OUTPUT_AUTO_SAVE_THRESHOLD_BYTES,
    ) -> ToolOutputFilePolicy:
        """Build a policy using the runtime-visible byte cap."""
        return cls(
            workspace_root=workspace_root,
            max_bytes=_output_redirect_max_bytes(runtime_paths),
            auto_save_threshold_bytes=auto_save_threshold_bytes,
        )


@dataclass(frozen=True)
class _ValidatedOutputPath:
    requested_path: str
    relative_path: Path
    absolute_path: Path
    overwritten: bool


@dataclass(frozen=True)
class ToolOutputFileRequest:
    """Validated output destination for a direct or externally executed tool call."""

    policy: ToolOutputFilePolicy
    tool_name: str
    path: _ValidatedOutputPath | None


@dataclass(frozen=True)
class ToolOutputFileHandled:
    """A tool owner has accepted responsibility for publishing its output file."""

    result: object


_active_output_request: ContextVar[ToolOutputFileRequest | None] = ContextVar("tool_output_file_request", default=None)


def current_tool_output_file_request() -> ToolOutputFileRequest | None:
    """Return the current explicit redirect, for tools that capture before returning."""
    request = _active_output_request.get()
    return request if request is not None and request.path is not None else None


@dataclass(frozen=True)
class _SerializedToolOutput:
    payload: bytes
    format: Literal["text", "json", "binary"]


@dataclass(frozen=True)
class _ToolOutputWriteResult:
    """Result from writing caller-managed bytes to a validated output path."""

    receipt: dict[str, object]
    absolute_path: Path
    byte_count: int
    overwritten: bool


def _output_redirect_max_bytes(runtime_paths: RuntimePaths) -> int:
    raw_value = runtime_paths.env_value(_MAX_BYTES_ENV)
    if not raw_value:
        return DEFAULT_TOOL_OUTPUT_MAX_BYTES
    try:
        configured_value = int(raw_value)
    except ValueError:
        configured_value = 0
    if configured_value <= 0:
        logger.warning("invalid_tool_output_redirect_max_bytes", value=raw_value)
        return DEFAULT_TOOL_OUTPUT_MAX_BYTES
    return configured_value


def _success_receipt(
    *,
    path: str,
    byte_count: int,
    output_format: Literal["text", "json", "binary"],
    overwritten: bool,
) -> dict[str, object]:
    return {
        "mindroom_tool_output": saved_tool_output_receipt(
            path=path,
            byte_count=byte_count,
            output_format=output_format,
            overwritten=overwritten,
        ),
    }


def _auto_save_success_receipt(
    *,
    path: str,
    byte_count: int,
    output_format: Literal["text", "json", "binary"],
    threshold_bytes: int,
    preview: str,
) -> dict[str, object]:
    receipt = saved_tool_output_receipt(
        path=path,
        byte_count=byte_count,
        output_format=output_format,
        overwritten=False,
    )
    receipt.update(auto_saved=True, threshold_bytes=threshold_bytes, preview=preview, hint=_AUTO_SAVE_READ_HINT)
    return {"mindroom_tool_output": receipt}


def saved_tool_output_receipt(
    *,
    path: str,
    byte_count: int,
    output_format: Literal["text", "json", "binary"],
    overwritten: bool | None = None,
    sha256: str | None = None,
) -> dict[str, object]:
    """Return the canonical inner ``mindroom_tool_output`` saved-file receipt."""
    receipt: dict[str, object] = {
        "status": "saved_to_file",
        "path": path,
        "bytes": byte_count,
        "format": output_format,
    }
    if overwritten is not None:
        receipt["overwritten"] = overwritten
    if sha256 is not None:
        receipt["sha256"] = sha256
    return receipt


def _error_receipt(error: str) -> dict[str, object]:
    return {
        "mindroom_tool_output": {
            "status": "error",
            "error": error,
        },
    }


def _output_path_schema() -> dict[str, object]:
    return {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "default": None,
        "description": _OUTPUT_PATH_ARGUMENT_DESCRIPTION,
    }


def with_optional_output_path(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``parameters`` offering MindRoom's reserved output-path argument as its last, optional property."""
    finished = dict(parameters)
    properties = dict(finished.get("properties") or {})
    properties[OUTPUT_PATH_ARGUMENT] = _output_path_schema()
    finished["type"] = finished.get("type") or "object"
    finished["properties"] = properties
    required = finished.get("required")
    finished["required"] = (
        [name for name in required if name != OUTPUT_PATH_ARGUMENT] if isinstance(required, list) else []
    )
    return finished


def normalize_output_path_argument(raw_path: object) -> object | None:
    """Return the canonical runtime value for the reserved output-path argument."""
    if isinstance(raw_path, str) and raw_path.strip() == "":
        return None
    return raw_path


def _path_has_environment_expansion(raw_path: str) -> bool:
    return raw_path.startswith("~") or "$" in raw_path or "%" in raw_path


def _relative_output_path_error(relative_path: Path) -> str | None:
    if relative_path.is_absolute():
        return "mindroom_output_path must be relative to the workspace."
    if relative_path == Path():
        return "mindroom_output_path must name a file, not the workspace root."
    if any(part == ".." for part in relative_path.parts):
        return "mindroom_output_path must stay inside the workspace."
    if is_git_metadata_path(relative_path):
        return "mindroom_output_path must not target Git metadata ('.git')."
    return None


def _validate_raw_output_path(raw_path: object) -> tuple[str, Path] | str:
    if not isinstance(raw_path, str):
        return "mindroom_output_path must be a workspace-relative string path."
    if raw_path.strip() == "":
        return "mindroom_output_path must be a non-empty workspace-relative path."
    if "\x00" in raw_path:
        return "mindroom_output_path must not contain NUL bytes."
    if _path_has_environment_expansion(raw_path):
        return (
            f"mindroom_output_path must be a plain path relative to the agent workspace, but got {raw_path!r}. "
            "Paths must not start with `~` or contain `$` or `%`; pass a workspace-relative path like 'output.json', "
            "then copy the file elsewhere with shell tools if needed."
        )
    relative_path = Path(raw_path)
    error = _relative_output_path_error(relative_path)
    return error if error is not None else (raw_path, relative_path)


def _validate_output_path(policy: ToolOutputFilePolicy, raw_path: object) -> _ValidatedOutputPath | str:
    raw_validation = _validate_raw_output_path(raw_path)
    if isinstance(raw_validation, str):
        return raw_validation

    requested_path, relative_path = raw_validation
    try:
        candidate = resolve_relative_path_within_root_preserving_leaf(
            policy.workspace_root,
            relative_path,
            field_name=OUTPUT_PATH_ARGUMENT,
            root_label="workspace root",
        )
    except ValueError:
        return "mindroom_output_path must stay inside the workspace."

    parent_error = _validate_parent_components(policy.workspace_root, relative_path.parent)
    if parent_error is not None:
        return parent_error
    if candidate.is_symlink():
        return "mindroom_output_path must not target a symlink."
    if candidate.exists() and candidate.is_dir():
        return "mindroom_output_path must name a file, not an existing directory."
    return _ValidatedOutputPath(
        requested_path=requested_path,
        relative_path=relative_path,
        absolute_path=candidate,
        overwritten=candidate.exists(),
    )


def validate_output_path(policy: ToolOutputFilePolicy, raw_path: object) -> str | None:
    """Validate one output path without creating parent directories or writing bytes."""
    validation = _validate_output_path(policy, raw_path)
    return validation if isinstance(validation, str) else None


def validate_output_path_syntax(raw_path: object) -> str | None:
    """Validate output-path syntax when the destination workspace is remote."""
    validation = _validate_raw_output_path(raw_path)
    return validation if isinstance(validation, str) else None


def _validate_parent_components(workspace_root: Path, relative_parent: Path) -> str | None:
    """Retain directory-type diagnostics after shared symlink validation."""
    message = "mindroom_output_path parent components must be directories."
    try:
        mode = (workspace_root.expanduser() / relative_parent).stat().st_mode
    except FileNotFoundError:
        return None
    except NotADirectoryError:
        return message
    return None if stat.S_ISDIR(mode) else message


def _normalize_json_value(value: object) -> object:  # noqa: PLR0911 - One branch per supported value kind.
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return _normalize_json_value(value.value)
    if isinstance(value, BaseModel):
        return _normalize_json_value(value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return _normalize_json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _normalize_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_normalize_json_value(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted((_normalize_json_value(item) for item in value), key=repr)
    msg = f"Value is not JSON-normalizable: {type(value).__name__}"
    raise TypeError(msg)


def _serialize_tool_output(result: object) -> _SerializedToolOutput | str:
    # Deferred because the toolkit adapter, which reads a vendor tool's value, imports this module.
    from mindroom.tool_system.agno_toolkits import read_tool_value  # noqa: PLC0415

    if (value := read_tool_value(result)) is not None:
        if any(value.media()):
            return "Tool media results cannot be redirected to workspace files."
        serialized = _SerializedToolOutput(payload=value.content.encode("utf-8"), format="text")
    elif isinstance(result, str):
        serialized = _SerializedToolOutput(payload=result.encode("utf-8"), format="text")
    elif isinstance(result, bytes | bytearray | memoryview):
        return "Binary tool outputs cannot be redirected to workspace files."
    elif inspect.isgenerator(result) or inspect.isasyncgen(result) or isinstance(result, Iterator):
        return "Generator or stream tool outputs cannot be redirected to workspace files."
    else:
        try:
            normalized = _normalize_json_value(result)
            payload = json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
            serialized = _SerializedToolOutput(payload=payload, format="json")
        except (TypeError, ValueError):
            serialized = _SerializedToolOutput(
                payload=f"{_TEXT_FALLBACK_HEADER}{result}".encode(),
                format="text",
            )
    return serialized


def _preview_payload(payload: bytes) -> str:
    preview_bytes = payload[:_AUTO_SAVE_PREVIEW_BYTES]
    preview = preview_bytes.decode("utf-8", errors="replace").rstrip()
    if len(payload) <= _AUTO_SAVE_PREVIEW_BYTES:
        return preview
    return f"{preview}\n\n[Preview truncated. Full tool output was saved to file.]"


def _safe_tool_name(tool_name: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in tool_name.strip())
    return safe[:80] or "tool"


def _auto_output_relative_path(tool_name: str, output_format: Literal["text", "json", "binary"]) -> str:
    suffix = {"text": "txt", "json": "json", "binary": "bin"}[output_format]
    return f"{_AUTO_SAVE_ROOT}/{_safe_tool_name(tool_name)}-{uuid.uuid4().hex}.{suffix}"


def _write_atomic(
    payload: bytes,
    workspace_root: Path,
    relative_path: Path,
    *,
    file_mode: int | None = None,
) -> str | None:
    try:
        # Open the workspace as spelled so a workspace replaced by a link is refused, not followed.
        with open_directory_within_root(
            workspace_root.expanduser(),
            relative_path.parent,
            create=True,
        ) as directory_fd:
            atomic_write_bytes_at(directory_fd, relative_path.name, payload, file_mode=file_mode)
    except OSError as exc:
        logger.warning("tool_output_redirect_write_failed", error_type=type(exc).__name__)
        return "Failed to write redirected tool output."
    return None


def write_bytes_to_output_path(
    policy: ToolOutputFilePolicy,
    raw_path: object,
    payload: bytes,
    *,
    output_format: Literal["binary"] = "binary",
    file_mode: int | None = None,
) -> _ToolOutputWriteResult | str:
    """Validate and atomically write caller-owned bytes to a MindRoom output path."""
    validated_path = _validate_output_path(policy, raw_path)
    if isinstance(validated_path, str):
        return validated_path

    byte_count = len(payload)
    if byte_count > policy.max_bytes:
        return f"Redirected tool output is {byte_count} bytes, which exceeds the {policy.max_bytes} byte limit."

    write_error = _write_atomic(
        payload,
        policy.workspace_root,
        validated_path.relative_path,
        file_mode=file_mode,
    )
    if write_error is not None:
        return write_error

    return _ToolOutputWriteResult(
        receipt=_success_receipt(
            path=validated_path.requested_path,
            byte_count=byte_count,
            output_format=output_format,
            overwritten=validated_path.overwritten,
        ),
        absolute_path=validated_path.absolute_path,
        byte_count=byte_count,
        overwritten=validated_path.overwritten,
    )


def _redirect_result_to_file(
    result: object,
    *,
    policy: ToolOutputFilePolicy,
    validated_path: _ValidatedOutputPath,
) -> dict[str, object]:
    serialized = _serialize_tool_output(result)
    if isinstance(serialized, str):
        return _error_receipt(serialized)

    byte_count = len(serialized.payload)
    if byte_count > policy.max_bytes:
        return _error_receipt(
            f"Redirected tool output is {byte_count} bytes, which exceeds the {policy.max_bytes} byte limit.",
        )

    write_error = _write_atomic(
        serialized.payload,
        policy.workspace_root,
        validated_path.relative_path,
    )
    if write_error is not None:
        return _error_receipt(write_error)

    return _success_receipt(
        path=validated_path.requested_path,
        byte_count=byte_count,
        output_format=serialized.format,
        overwritten=validated_path.overwritten,
    )


def _auto_save_large_result(
    result: object,
    *,
    policy: ToolOutputFilePolicy,
    tool_name: str,
) -> object:
    try:
        serialized = _serialize_tool_output(result)
    except Exception as exc:
        logger.warning("tool_output_auto_save_serialization_failed", error_type=type(exc).__name__)
        return result
    if isinstance(serialized, str) or len(serialized.payload) <= policy.auto_save_threshold_bytes:
        return result
    try:
        return _write_auto_saved_result(serialized, policy=policy, tool_name=tool_name)
    except Exception as exc:
        logger.warning("tool_output_auto_save_write_failed", error_type=type(exc).__name__)
        return result


def _write_auto_saved_result(
    serialized: _SerializedToolOutput,
    *,
    policy: ToolOutputFilePolicy,
    tool_name: str,
) -> object:
    byte_count = len(serialized.payload)
    if byte_count > policy.max_bytes:
        return _error_receipt(
            f"Tool output is {byte_count} bytes, which exceeds the {policy.max_bytes} byte auto-save limit.",
        )

    relative_path = _auto_output_relative_path(tool_name, serialized.format)
    validated_path = _validate_output_path(policy, relative_path)
    if isinstance(validated_path, str):
        return _error_receipt(validated_path)

    write_error = _write_atomic(
        serialized.payload,
        policy.workspace_root,
        validated_path.relative_path,
    )
    if write_error is not None:
        return _error_receipt(write_error)

    return _auto_save_success_receipt(
        path=validated_path.requested_path,
        byte_count=byte_count,
        output_format=serialized.format,
        threshold_bytes=policy.auto_save_threshold_bytes,
        preview=_preview_payload(serialized.payload),
    )


def prepare_tool_output_file(
    policy: ToolOutputFilePolicy,
    *,
    tool_name: str,
    output_path: object = None,
) -> ToolOutputFileRequest | dict[str, object]:
    """Validate before tool execution, without creating files or directories."""
    normalized = normalize_output_path_argument(output_path)
    validated = _validate_output_path(policy, normalized) if normalized is not None else None
    if isinstance(validated, str):
        return _error_receipt(validated)
    return ToolOutputFileRequest(policy=policy, tool_name=tool_name, path=validated)


def finalize_tool_output_file(request: ToolOutputFileRequest, result: object) -> object:
    """Apply the shared explicit redirect or large-result policy after execution."""
    if isinstance(result, ToolOutputFileHandled):
        return result.result
    if request.path is None:
        return _auto_save_large_result(result, policy=request.policy, tool_name=request.tool_name)
    return _redirect_result_to_file(result, policy=request.policy, validated_path=request.path)


def with_output_files(function: ToolFunction, policy: ToolOutputFilePolicy) -> ToolFunction:
    """Return ``function`` also taking and handling ``mindroom_output_path``, its unset ``strict`` made False.

    Its schema offers the argument as its last, optional property. The entrypoint takes the argument out of each call,
    runs the call with the rest, and saves or redirects its output. A strict function keeps its schema as its author
    made it, and a function that takes the argument itself, by its entrypoint or its schema, stays as it is too.
    """
    parameters = inspect.signature(function.entrypoint).parameters
    properties = function.parameters.get("properties")
    if function.strict:
        return function
    if OUTPUT_PATH_ARGUMENT in parameters or (isinstance(properties, Mapping) and OUTPUT_PATH_ARGUMENT in properties):
        logger.debug(
            "tool_output_path_argument_collision",
            function_name=function.name,
            argument_name=OUTPUT_PATH_ARGUMENT,
        )
        return function
    return replace(
        function,
        parameters=with_optional_output_path(function.parameters),
        strict=False,
        entrypoint=_handling_output_files(function.entrypoint, policy, tool_name=function.name),
    )


def _handling_output_files(
    entrypoint: Callable[..., object],
    policy: ToolOutputFilePolicy,
    *,
    tool_name: str,
) -> Callable[..., object]:
    """Return ``entrypoint`` taking ``mindroom_output_path`` out of each call and saving or redirecting its output.

    A synchronous entrypoint stays synchronous, so preparing the path, the call, and writing the file share its thread.
    """
    if inspect.iscoroutinefunction(entrypoint):
        async_entrypoint = cast("Callable[..., Awaitable[object]]", entrypoint)

        @functools.wraps(entrypoint)
        async def async_wrapper(*args: object, mindroom_output_path: object = None, **kwargs: object) -> object:
            request = prepare_tool_output_file(policy, tool_name=tool_name, output_path=mindroom_output_path)
            if isinstance(request, dict):
                return request
            token = _active_output_request.set(request)
            try:
                result = await async_entrypoint(*args, **kwargs)
            finally:
                _active_output_request.reset(token)
            return finalize_tool_output_file(request, result)

        return async_wrapper

    @functools.wraps(entrypoint)
    def sync_wrapper(*args: object, mindroom_output_path: object = None, **kwargs: object) -> object:
        request = prepare_tool_output_file(policy, tool_name=tool_name, output_path=mindroom_output_path)
        if isinstance(request, dict):
            return request
        token = _active_output_request.set(request)
        try:
            result = entrypoint(*args, **kwargs)
        finally:
            _active_output_request.reset(token)
        return finalize_tool_output_file(request, result)

    return sync_wrapper
