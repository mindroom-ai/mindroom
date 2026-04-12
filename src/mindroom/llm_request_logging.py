"""Opt-in LLM request logging."""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from agno.models.message import Message
from pydantic import BaseModel

from mindroom.constants import MATRIX_SOURCE_EVENT_IDS_METADATA_KEY, MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Coroutine, Iterator, Sequence

    from agno.models.base import Model
    from agno.models.response import ModelResponse

    from mindroom.config.models import DebugConfig

_INSTALLED_ATTR = "_mindroom_llm_request_logging_installed"
_SKIP_MODEL_PARAM_NAMES = {
    "id",
    "name",
    "provider",
    "model_type",
    "system_prompt",
    "instructions",
    "client",
    "async_client",
    "api_key",
    "auth_token",
    "organization",
    "http_client",
    "client_params",
    "default_headers",
    "default_query",
}
_NON_API_MESSAGE_FIELDS = {
    "id",
    "reasoning_content",
    "tool_name",
    "tool_args",
    "tool_call_error",
    "stop_after_tool_call",
    "add_to_agent_memory",
    "from_history",
    "references",
    "temporary",
}
type JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
_REQUEST_CONTEXT = ContextVar[dict[str, JSONValue] | None]("mindroom_llm_request_log_context", default=None)


def _daily_log_path(log_dir: str | None, default_log_dir: Path, now: datetime) -> Path:
    base_dir = Path(log_dir) if log_dir else default_log_dir
    return base_dir / f"llm-requests-{now.date().isoformat()}.jsonl"


def _system_prompt(messages: Sequence[Message], model: Model) -> str:
    for message in messages:
        if message.role == "system":
            return message.get_content_string()
    return model.system_prompt or ""


def _model_params(model: Model) -> dict[str, JSONValue]:
    if not is_dataclass(model):
        return {}
    payload: dict[str, JSONValue] = {}
    for field in fields(model):
        if field.name in _SKIP_MODEL_PARAM_NAMES:
            continue
        value = vars(model).get(field.name)
        if value is None:
            continue
        try:
            json.dumps(value)
        except TypeError:
            continue
        payload[field.name] = value
    return payload


def _write_jsonl_line(path: Path, payload: dict[str, JSONValue]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload))
        handle.write("\n")


def _json_safe(value: object) -> JSONValue:
    normalized: object = value
    if isinstance(normalized, BaseModel):
        normalized = normalized.model_dump(mode="python", exclude_none=True)
    elif not isinstance(normalized, type) and is_dataclass(normalized):
        normalized = asdict(normalized)

    if isinstance(normalized, dict):
        return {str(key): _json_safe(item) for key, item in normalized.items()}
    if isinstance(normalized, (list, tuple, set)):
        return [_json_safe(item) for item in normalized]
    if isinstance(normalized, bytes):
        return {
            "__type__": "bytes",
            "base64": base64.b64encode(normalized).decode("ascii"),
        }
    if isinstance(normalized, Path):
        return str(normalized)
    if isinstance(normalized, str | int | float | bool) or normalized is None:
        return normalized
    return repr(normalized)


def _request_message_payloads(messages: Sequence[Message]) -> list[dict[str, JSONValue]]:
    payloads: list[dict[str, JSONValue]] = []
    for message in messages:
        payload = message.model_dump(
            mode="python",
            exclude_none=True,
            exclude=_NON_API_MESSAGE_FIELDS,
        )
        payloads.append(cast("dict[str, JSONValue]", _json_safe(payload)))
    return payloads


def _request_messages(value: object) -> list[Message] | None:
    if isinstance(value, list) and all(isinstance(message, Message) for message in value):
        return cast("list[Message]", value)
    return None


def _request_tools(value: object) -> list[dict[str, JSONValue]] | None:
    if isinstance(value, list) and all(isinstance(tool, dict) for tool in value):
        return cast("list[dict[str, JSONValue]]", value)
    return None


def _normalized_string_list(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for value in values:
        if isinstance(value, str) and value and value not in normalized:
            normalized.append(value)
    return normalized


def _snapshot_request_log_context() -> dict[str, JSONValue]:
    """Return one detached copy of the currently bound request log context."""
    return cast("dict[str, JSONValue]", _json_safe(_REQUEST_CONTEXT.get() or {}))


def build_llm_request_log_context(
    *,
    session_id: str,
    room_id: str | None,
    thread_id: str | None,
    reply_to_event_id: str | None,
    prompt: str,
    model_prompt: str | None,
    full_prompt: str,
    metadata: dict[str, object] | None,
) -> dict[str, object]:
    """Build explicit per-request log context for one provider call."""
    context: dict[str, object] = {
        "session_id": session_id,
        "current_turn_prompt": prompt,
        "full_prompt": full_prompt,
    }
    if room_id:
        context["room_id"] = room_id
    if thread_id:
        context["thread_id"] = thread_id
    if reply_to_event_id:
        context["reply_to_event_id"] = reply_to_event_id
    if model_prompt is not None:
        context["model_prompt"] = model_prompt
    if not metadata:
        return context

    source_event_ids = _normalized_string_list(
        [
            reply_to_event_id,
            *_normalized_string_list(metadata.get(MATRIX_SOURCE_EVENT_IDS_METADATA_KEY)),
        ],
    )
    if source_event_ids:
        context["source_event_ids"] = source_event_ids

    raw_prompt_map = metadata.get(MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY)
    if isinstance(raw_prompt_map, dict):
        source_event_prompts = {
            event_id: event_prompt
            for event_id, event_prompt in raw_prompt_map.items()
            if isinstance(event_id, str) and event_id and isinstance(event_prompt, str)
        }
        if source_event_prompts:
            context["source_event_prompts"] = source_event_prompts

    return context
@contextmanager
def bind_llm_request_log_context(**context: object) -> Iterator[None]:
    """Bind per-run request metadata so log entries can be attributed later."""
    existing_context = _REQUEST_CONTEXT.get() or {}
    bound_context = dict(existing_context)
    for key, value in context.items():
        if value is None:
            continue
        bound_context[str(key)] = _json_safe(value)
    token = _REQUEST_CONTEXT.set(bound_context or None)
    try:
        yield
    finally:
        _REQUEST_CONTEXT.reset(token)


async def write_llm_request_log(
    *,
    model: Model,
    agent_name: str,
    messages: Sequence[Message],
    tools: list[dict[str, JSONValue]] | None,
    log_dir: str | None,
    default_log_dir: Path,
    request_context: dict[str, JSONValue] | None = None,
) -> None:
    """Persist one request record for an LLM invocation."""
    now = datetime.now().astimezone()
    resolved_request_context = request_context if request_context is not None else _snapshot_request_log_context()
    await asyncio.to_thread(
        _write_jsonl_line,
        _daily_log_path(log_dir, default_log_dir, now),
        {
            "timestamp": now.isoformat(),
            **resolved_request_context,
            "agent_name": agent_name,
            "model_id": model.id,
            "system_prompt": _system_prompt(messages, model),
            "messages": _request_message_payloads(messages),
            "message_count": len(messages),
            "tools": _json_safe(tools),
            "tool_count": len(tools or []),
            "model_params": _model_params(model),
        },
    )


async def _write_llm_request_log_if_present(
    *,
    model: Model,
    agent_name: str,
    kwargs: dict[str, object],
    log_dir: str | None,
    default_log_dir: Path,
    request_context: dict[str, JSONValue],
) -> None:
    """Write one request log entry when provider kwargs include API request messages."""
    messages = _request_messages(kwargs.get("messages"))
    if messages is None:
        return
    await write_llm_request_log(
        model=model,
        agent_name=agent_name,
        messages=messages,
        tools=_request_tools(kwargs.get("tools")),
        log_dir=log_dir,
        default_log_dir=default_log_dir,
        request_context=request_context,
    )


def install_llm_request_logging(
    model: Model,
    *,
    agent_name: str,
    debug_config: DebugConfig,
    default_log_dir: Path,
) -> None:
    """Wrap one model instance so request summaries are written before invocation."""
    if not debug_config.log_llm_requests:
        return
    model_dict = vars(model)
    if model_dict.get(_INSTALLED_ATTR) is True:
        return

    original_ainvoke = model.ainvoke
    original_ainvoke_stream = model.ainvoke_stream

    def _logged_ainvoke(*args: object, **kwargs: object) -> Coroutine[object, object, ModelResponse]:
        request_context = _snapshot_request_log_context()

        async def _invoke() -> ModelResponse:
            await _write_llm_request_log_if_present(
                model=model,
                agent_name=agent_name,
                kwargs=kwargs,
                log_dir=debug_config.llm_request_log_dir,
                default_log_dir=default_log_dir,
                request_context=request_context,
            )
            return await original_ainvoke(*args, **kwargs)

        return _invoke()

    def _logged_ainvoke_stream(*args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
        request_context = _snapshot_request_log_context()

        async def _stream() -> AsyncIterator[ModelResponse]:
            await _write_llm_request_log_if_present(
                model=model,
                agent_name=agent_name,
                kwargs=kwargs,
                log_dir=debug_config.llm_request_log_dir,
                default_log_dir=default_log_dir,
                request_context=request_context,
            )
            async for chunk in original_ainvoke_stream(*args, **kwargs):
                yield chunk

        return _stream()

    model_dict["ainvoke"] = _logged_ainvoke
    model_dict["ainvoke_stream"] = _logged_ainvoke_stream
    model_dict[_INSTALLED_ATTR] = True
