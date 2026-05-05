"""Central support for redirecting tool output into workspace files."""

from __future__ import annotations

import inspect
import json
import os
import tempfile
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from pathlib import Path
from types import MethodType
from typing import TYPE_CHECKING, Any, Literal, cast, get_type_hints

from agno.tools.function import Function, ToolResult
from pydantic import BaseModel

from mindroom.logging_config import get_logger
from mindroom.workspaces import resolve_relative_path_within_root_preserving_leaf

if TYPE_CHECKING:
    from agno.tools import Toolkit

    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

OUTPUT_PATH_ARGUMENT = "mindroom_output_path"
OUTPUT_PATH_ARGUMENT_DESCRIPTION = (
    "Optional MindRoom-managed workspace-relative path. "
    "If set, the full supported tool output is written to this file in your workspace and the tool returns only a "
    "compact receipt. "
    "Use this for large output you plan to inspect later with file, coding, python, or shell tools."
)
_MAX_BYTES_ENV = "MINDROOM_TOOL_OUTPUT_REDIRECT_MAX_BYTES"
_DEFAULT_MAX_BYTES = 64 * 1024 * 1024
_WRAPPED_ATTR = "__mindroom_output_file_wrapped__"
_DEFAULT_PARAMETERS = {"type": "object", "properties": {}, "required": []}
_TEXT_FALLBACK_HEADER = (
    "MindRoom tool output serialized with text fallback because the result was not JSON-normalizable.\n\n"
)


@dataclass(frozen=True)
class ToolOutputFilePolicy:
    """Resolved policy for one toolkit's model-requested output files."""

    workspace_root: Path
    max_bytes: int = _DEFAULT_MAX_BYTES

    @classmethod
    def from_runtime(
        cls,
        workspace_root: Path,
        runtime_paths: RuntimePaths,
    ) -> ToolOutputFilePolicy:
        """Build a policy using the runtime-visible byte cap."""
        return cls(
            workspace_root=workspace_root,
            max_bytes=_output_redirect_max_bytes(runtime_paths),
        )


@dataclass(frozen=True)
class _ValidatedOutputPath:
    requested_path: str
    relative_path: Path
    absolute_path: Path
    overwritten: bool


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
    raw_value = (
        runtime_paths.process_env.get(_MAX_BYTES_ENV)
        or runtime_paths.env_file_values.get(_MAX_BYTES_ENV)
        or os.environ.get(_MAX_BYTES_ENV)
    )
    if raw_value is None or raw_value == "":
        return _DEFAULT_MAX_BYTES
    try:
        max_bytes = int(raw_value)
    except ValueError:
        logger.warning("invalid_tool_output_redirect_max_bytes", value=raw_value)
        return _DEFAULT_MAX_BYTES
    if max_bytes <= 0:
        logger.warning("invalid_tool_output_redirect_max_bytes", value=raw_value)
        return _DEFAULT_MAX_BYTES
    return max_bytes


def _success_receipt(
    *,
    path: str,
    byte_count: int,
    output_format: Literal["text", "json", "binary"],
    overwritten: bool,
) -> dict[str, object]:
    return {
        "mindroom_tool_output": {
            "status": "saved_to_file",
            "path": path,
            "bytes": byte_count,
            "format": output_format,
            "overwritten": overwritten,
        },
    }


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
        "description": OUTPUT_PATH_ARGUMENT_DESCRIPTION,
    }


def _has_output_path_argument(function: Function) -> bool:
    if function.entrypoint is not None and OUTPUT_PATH_ARGUMENT in inspect.signature(function.entrypoint).parameters:
        return True
    properties = function.parameters.get("properties")
    return isinstance(properties, dict) and OUTPUT_PATH_ARGUMENT in properties


def _ensure_output_path_schema_optional(function: Function) -> None:
    """Keep MindRoom's reserved output-path argument optional in one tool schema."""
    parameters = dict(function.parameters or _DEFAULT_PARAMETERS)
    properties = dict(parameters.get("properties") or {})
    properties[OUTPUT_PATH_ARGUMENT] = _output_path_schema()
    parameters["type"] = parameters.get("type") or "object"
    parameters["properties"] = properties
    required = parameters.get("required")
    if isinstance(required, list):
        parameters["required"] = [name for name in required if name != OUTPUT_PATH_ARGUMENT]
    else:
        parameters["required"] = []
    function.parameters = parameters


def _normalize_output_path_argument(raw_path: object) -> object | None:
    if raw_path is None:
        return None
    if isinstance(raw_path, str) and raw_path.strip() == "":
        return None
    return raw_path


def normalize_output_path_argument(raw_path: object) -> object | None:
    """Return the canonical runtime value for the reserved output-path argument."""
    return _normalize_output_path_argument(raw_path)


def _process_entrypoint_with_output_path_schema(self: Function, strict: bool = False) -> None:
    effective_strict = False if self.strict is False else strict
    Function.process_entrypoint(self, strict=effective_strict)
    _ensure_output_path_schema_optional(self)


def _copy_function_model(self: Function, *, update: Mapping[str, object] | None, deep: bool) -> Function:
    model_copy_parameters = inspect.signature(Function.model_copy).parameters
    if "update" in model_copy_parameters:
        copied = cast("Any", Function.model_copy)(self, update=update, deep=deep)
    else:
        copied = Function.model_copy(self, deep=deep)
        if update:
            for field_name, value in update.items():
                object.__setattr__(copied, field_name, value)
    return copied


def _model_copy_with_output_path_schema(
    self: Function,
    *,
    update: Mapping[str, object] | None = None,
    deep: bool = False,
) -> Function:
    copied = _copy_function_model(self, update=update, deep=deep)
    _install_output_path_schema_postprocessor(copied)
    return copied


def _install_output_path_schema_postprocessor(function: Function) -> None:
    """Install a per-function schema sanitizer that survives Agno's Function copies."""
    object.__setattr__(
        function,
        "process_entrypoint",
        MethodType(_process_entrypoint_with_output_path_schema, function),
    )
    object.__setattr__(
        function,
        "model_copy",
        MethodType(_model_copy_with_output_path_schema, function),
    )


def _path_has_environment_expansion(raw_path: str) -> bool:
    return raw_path.startswith("~") or "$" in raw_path or "%" in raw_path


def _validate_raw_output_path(raw_path: object) -> tuple[str, Path] | str:
    error: str | None = None
    relative_path: Path | None = None
    if not isinstance(raw_path, str):
        error = "mindroom_output_path must be a workspace-relative string path."
    elif raw_path == "" or raw_path.strip() == "":
        error = "mindroom_output_path must be a non-empty workspace-relative path."
    elif "\x00" in raw_path:
        error = "mindroom_output_path must not contain NUL bytes."
    elif _path_has_environment_expansion(raw_path):
        error = "mindroom_output_path must not use environment or user expansion."
    else:
        relative_path = Path(raw_path)
        if relative_path.is_absolute():
            error = "mindroom_output_path must be relative to the workspace."
        elif relative_path == Path():
            error = "mindroom_output_path must name a file, not the workspace root."
        elif any(part == ".." for part in relative_path.parts):
            error = "mindroom_output_path must stay inside the workspace."

    if error is not None:
        return error
    if not isinstance(raw_path, str) or relative_path is None:
        return "mindroom_output_path must be a workspace-relative string path."
    return raw_path, relative_path


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
    """Reject existing unsafe parent components without creating anything."""
    if relative_parent == Path():
        return None

    current = workspace_root.expanduser()
    for part in relative_parent.parts:
        current = current / part
        if component_error := _existing_parent_component_error(current):
            return component_error
    return None


def _existing_parent_component_error(path: Path) -> str | None:
    if path.is_symlink():
        return "mindroom_output_path parent must stay inside the workspace."
    if path.exists() and not path.is_dir():
        return "mindroom_output_path parent components must be directories."
    return None


def _ensure_parent_directory(workspace_root: Path, relative_parent: Path) -> str | None:
    """Create parent directories one component at a time without accepting symlinks."""
    resolved_root = workspace_root.resolve()
    current = workspace_root.expanduser()
    for part in relative_parent.parts:
        current = current / part
        if component_error := _existing_parent_component_error(current):
            return component_error
        try:
            current.mkdir()
        except FileExistsError:
            if component_error := _existing_parent_component_error(current):
                return component_error
        except OSError:
            return "Failed to prepare redirected tool output path."

        try:
            resolved_current = current.resolve()
        except OSError:
            return "Failed to prepare redirected tool output path."
        if not resolved_current.is_relative_to(resolved_root):
            return "mindroom_output_path parent escaped the workspace before write."
    return None


def _normalize_json_value(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        normalized = value
    elif isinstance(value, Path):
        normalized = str(value)
    elif isinstance(value, Enum):
        normalized = _normalize_json_value(value.value)
    elif isinstance(value, BaseModel):
        normalized = _normalize_json_value(value.model_dump(mode="json"))
    elif is_dataclass(value) and not isinstance(value, type):
        normalized = _normalize_json_value(asdict(value))
    elif isinstance(value, Mapping):
        normalized = {str(key): _normalize_json_value(item) for key, item in value.items()}
    elif isinstance(value, tuple | list):
        normalized = [_normalize_json_value(item) for item in value]
    elif isinstance(value, set | frozenset):
        normalized_items = [_normalize_json_value(item) for item in value]
        normalized = sorted(normalized_items, key=repr)
    else:
        msg = f"Value is not JSON-normalizable: {type(value).__name__}"
        raise TypeError(msg)
    return normalized


def _has_tool_result_media(result: ToolResult) -> bool:
    return bool(result.images or result.videos or result.audios or result.files)


def _serialize_tool_output(result: object) -> _SerializedToolOutput | str:
    if isinstance(result, ToolResult):
        if _has_tool_result_media(result):
            return "ToolResult media artifacts cannot be redirected to workspace files in v1."
        serialized = _SerializedToolOutput(payload=result.content.encode("utf-8"), format="text")
    elif isinstance(result, str):
        serialized = _SerializedToolOutput(payload=result.encode("utf-8"), format="text")
    elif isinstance(result, bytes | bytearray | memoryview):
        return "Binary tool outputs cannot be redirected to workspace files in v1."
    elif inspect.isgenerator(result) or inspect.isasyncgen(result) or isinstance(result, Iterator):
        return "Generator or stream tool outputs cannot be redirected to workspace files in v1."
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


def _write_atomic(
    path: Path,
    payload: bytes,
    workspace_root: Path,
    relative_path: Path,
    *,
    file_mode: int | None = None,
) -> str | None:
    parent_error = _ensure_parent_directory(workspace_root, relative_path.parent)
    if parent_error is not None:
        return parent_error

    resolved_root = workspace_root.resolve()
    try:
        resolved_parent = path.parent.resolve()
    except OSError:
        return "Failed to prepare redirected tool output path."
    if not resolved_parent.is_relative_to(resolved_root):
        return "mindroom_output_path parent escaped the workspace before write."

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temp_file:
            temp_file.write(payload)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = Path(temp_file.name)
        if file_mode is not None:
            temp_path.chmod(file_mode)
        os.replace(temp_path, path)  # noqa: PTH105
    except OSError as exc:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
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
        validated_path.absolute_path,
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
        validated_path.absolute_path,
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


def _signature_with_output_path(entrypoint: Callable[..., object]) -> inspect.Signature:
    signature = inspect.signature(entrypoint)
    parameters = list(signature.parameters.values())
    output_parameter = inspect.Parameter(
        OUTPUT_PATH_ARGUMENT,
        inspect.Parameter.KEYWORD_ONLY,
        default=None,
        annotation=str | None,
    )
    var_keyword_index = next(
        (index for index, parameter in enumerate(parameters) if parameter.kind is inspect.Parameter.VAR_KEYWORD),
        len(parameters),
    )
    parameters.insert(var_keyword_index, output_parameter)
    return signature.replace(parameters=parameters)


def _copy_annotations_with_output_path(
    wrapper: Callable[..., object],
    original_entrypoint: Callable[..., object],
) -> None:
    try:
        annotations = dict(get_type_hints(original_entrypoint))
    except Exception:
        annotations = dict(getattr(original_entrypoint, "__annotations__", {}) or {})
    annotations[OUTPUT_PATH_ARGUMENT] = str | None
    wrapper.__annotations__ = annotations


def _docstring_with_output_path(original_doc: str | None) -> str:
    base = (original_doc or "MindRoom wrapped tool entrypoint.").strip()
    output_arg_doc = f"Args:\n    {OUTPUT_PATH_ARGUMENT}: {OUTPUT_PATH_ARGUMENT_DESCRIPTION}"
    return f"{base}\n\n{output_arg_doc}"


def _wrap_entrypoint(
    entrypoint: Callable[..., object],
    policy: ToolOutputFilePolicy,
) -> Callable[..., object]:
    if inspect.iscoroutinefunction(entrypoint):
        async_entrypoint = cast("Callable[..., Awaitable[object]]", entrypoint)

        async def async_wrapper(*args: object, mindroom_output_path: str | None = None, **kwargs: object) -> object:
            normalized_output_path = _normalize_output_path_argument(mindroom_output_path)
            if normalized_output_path is None:
                return await async_entrypoint(*args, **kwargs)
            validated_path = _validate_output_path(policy, normalized_output_path)
            if isinstance(validated_path, str):
                return _error_receipt(validated_path)
            result = await async_entrypoint(*args, **kwargs)
            return _redirect_result_to_file(result, policy=policy, validated_path=validated_path)

        wrapper = async_wrapper
    else:

        def sync_wrapper(*args: object, mindroom_output_path: str | None = None, **kwargs: object) -> object:
            normalized_output_path = _normalize_output_path_argument(mindroom_output_path)
            if normalized_output_path is None:
                return entrypoint(*args, **kwargs)
            validated_path = _validate_output_path(policy, normalized_output_path)
            if isinstance(validated_path, str):
                return _error_receipt(validated_path)
            result = entrypoint(*args, **kwargs)
            return _redirect_result_to_file(result, policy=policy, validated_path=validated_path)

        wrapper = sync_wrapper

    wrapper.__name__ = getattr(entrypoint, "__name__", "tool_entrypoint")
    wrapper.__doc__ = _docstring_with_output_path(getattr(entrypoint, "__doc__", None))
    wrapper.__module__ = getattr(entrypoint, "__module__", __name__)
    wrapper.__dict__["__signature__"] = _signature_with_output_path(entrypoint)
    _copy_annotations_with_output_path(wrapper, entrypoint)
    setattr(wrapper, _WRAPPED_ATTR, True)
    return wrapper


def _wrap_function_for_output_files(function: Function, policy: ToolOutputFilePolicy) -> Function:
    """Expose and handle ``mindroom_output_path`` on one Agno function."""
    if function.entrypoint is None or getattr(function.entrypoint, _WRAPPED_ATTR, False):
        return function
    if _has_output_path_argument(function):
        logger.warning(
            "tool_output_path_argument_collision",
            function_name=function.name,
            argument_name=OUTPUT_PATH_ARGUMENT,
        )
        return function

    uses_custom_parameters = function.skip_entrypoint_processing or function.parameters != _DEFAULT_PARAMETERS
    function.entrypoint = _wrap_entrypoint(function.entrypoint, policy)
    function.strict = False
    _install_output_path_schema_postprocessor(function)
    if uses_custom_parameters:
        _ensure_output_path_schema_optional(function)
    return function


def wrap_toolkit_for_output_files(
    toolkit: Toolkit,
    policy: ToolOutputFilePolicy | None,
) -> Toolkit:
    """Wrap every eligible function in a freshly-built toolkit."""
    if policy is None:
        return toolkit

    seen_functions: set[int] = set()
    for function in (*toolkit.functions.values(), *toolkit.async_functions.values()):
        if id(function) in seen_functions:
            continue
        seen_functions.add(id(function))
        _wrap_function_for_output_files(function, policy)
    return toolkit
