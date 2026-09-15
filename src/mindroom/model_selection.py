"""Leaf wire values for structured Matrix thread model commands."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, TypeGuard, cast

__all__ = [
    "MODEL_SELECTION_CONTENT_KEY",
    "MODEL_SELECTION_RESULT_CONTENT_KEY",
    "CommandResultContent",
    "ModelSelectionRequest",
    "command_result_content_to_dict",
    "freeze_command_result_content",
    "model_selection_result",
    "model_selection_targets_other_runtime",
    "parse_model_selection",
]

MODEL_SELECTION_CONTENT_KEY = "io.mindroom.model_selection"
MODEL_SELECTION_RESULT_CONTENT_KEY = "io.mindroom.model_selection_result"

type CommandResultContent = Mapping[str, Mapping[str, str | int | None]]


@dataclass(frozen=True, slots=True)
class ModelSelectionRequest:
    """Validated explicit operation addressed to one runtime device."""

    runtime_user_id: str
    runtime_device_id: str
    operation: Literal["set", "reset"]
    model: str | None = None
    version: Literal[1] = 1


def _bounded_string(value: object, limit: int) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def parse_model_selection(content: Mapping[str, object]) -> ModelSelectionRequest | None:
    """Return None only for absent metadata; malformed commands raise ValueError."""
    if MODEL_SELECTION_CONTENT_KEY not in content:
        return None
    raw = content[MODEL_SELECTION_CONTENT_KEY]
    error = "Invalid structured model selection. Refresh the model picker and retry."
    if not isinstance(raw, Mapping):
        raise ValueError(error)  # noqa: TRY004 - invalid wire metadata has one failure type.
    raw = cast("Mapping[str, object]", raw)
    operation = raw.get("operation")
    required = {"version", "runtime_user_id", "runtime_device_id", "operation"}
    expected = required | {"model"} if operation == "set" else required
    if (
        set(raw) != expected
        or type(raw.get("version")) is not int
        or raw["version"] != 1
        or operation not in ("set", "reset")
        or not _bounded_string(raw.get("runtime_user_id"), 1024)
        or not _bounded_string(raw.get("runtime_device_id"), 255)
        or (operation == "set" and not _bounded_string(raw.get("model"), 1024))
    ):
        raise ValueError(error)
    return ModelSelectionRequest(
        cast("str", raw["runtime_user_id"]),
        cast("str", raw["runtime_device_id"]),
        cast("Literal['set', 'reset']", operation),
        cast("str | None", raw.get("model")),
    )


def model_selection_targets_other_runtime(content: Mapping[str, object], user_id: str, device_id: str | None) -> bool:
    """Route recognizable target hints before validation or execution checkpoints."""
    raw = content.get(MODEL_SELECTION_CONTENT_KEY)
    if not isinstance(raw, Mapping):
        return False
    raw = cast("Mapping[str, object]", raw)
    return any(
        isinstance(raw.get(key), str) and raw[key] != actual
        for key, actual in (
            ("runtime_user_id", user_id),
            ("runtime_device_id", device_id),
        )
    )


def model_selection_result(
    request: ModelSelectionRequest,
    *,
    command_event_id: str,
    room_id: str,
    thread_id: str | None,
    error: str | None = None,
) -> dict[str, dict[str, str | int | None]]:
    """Build a correlated result only after validation and the persistence outcome."""
    result: dict[str, str | int | None] = {
        "version": 1,
        "command_event_id": command_event_id,
        "room_id": room_id,
        "thread_id": thread_id,
        "runtime_user_id": request.runtime_user_id,
        "runtime_device_id": request.runtime_device_id,
        "operation": request.operation,
        "status": "rejected" if error is not None else "applied",
    }
    if request.operation == "set":
        result["model"] = request.model
    if error is None:
        result["override"] = request.model if request.operation == "set" else None
    else:
        result["error"] = error
    return {MODEL_SELECTION_RESULT_CONTENT_KEY: result}


def freeze_command_result_content(raw: object) -> CommandResultContent | None:
    """Copy flat JSON result fields, excluding arbitrary and standard event content."""
    if not isinstance(raw, Mapping):
        return None
    raw = cast("Mapping[str, object]", raw)
    result = raw.get(MODEL_SELECTION_RESULT_CONTENT_KEY)
    if not isinstance(result, Mapping):
        return None
    result = cast("Mapping[str, object]", result)
    allowed = {
        "version",
        "command_event_id",
        "room_id",
        "thread_id",
        "runtime_user_id",
        "runtime_device_id",
        "operation",
        "model",
        "status",
        "override",
        "error",
    }
    copied = {
        key: value for key, value in result.items() if key in allowed and (value is None or type(value) in (str, int))
    }
    return MappingProxyType(
        {MODEL_SELECTION_RESULT_CONTENT_KEY: MappingProxyType(cast("dict[str, str | int | None]", copied))},
    )


def command_result_content_to_dict(content: CommandResultContent | None) -> dict | None:
    """Thaw immutable checkpoint data for the existing JSON outbox boundary."""
    return {key: dict(value) for key, value in content.items()} if content is not None else None
