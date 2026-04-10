"""Structured JSONL logging for pre-provider LLM request assembly data."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel

from mindroom.constants import resolve_config_relative_path
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Generator

    from agno.models.base import Model
    from agno.models.message import Message

    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_LLM_REQUEST_LOGGING_HOOK_ATTR = "_mindroom_llm_request_logging_hook_installed"
_WRITE_LOCK = asyncio.Lock()
_REDACTED = "***redacted***"
_SECRET_KEYS = (
    "access_token",
    "api_key",
    "api_secret",
    "authorization",
    "client_secret",
    "cookie",
    "password",
    "refresh_token",
    "secret",
    "token",
)

type JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class LLMRequestLoggingContext:
    """Dynamic request metadata bound around one agent or team run."""

    session_id: str | None = None
    room_id: str | None = None
    thread_id: str | None = None
    run_id: str | None = None
    agent_name: str | None = None
    model_config_name: str | None = None


@dataclass(frozen=True, slots=True)
class LLMRequestRecord:
    """One JSONL-safe record of pre-provider LLM request assembly data."""

    timestamp: str
    agent_name: str | None
    session_id: str | None
    room_id: str | None
    thread_id: str | None
    run_id: str | None
    provider: str
    model_config_name: str | None
    model_id: str
    system_prompt: JsonValue
    messages: JsonValue
    tools: JsonValue
    model_parameters: JsonValue
    cache_metadata: JsonValue

    def as_dict(self) -> dict[str, JsonValue]:
        """Return the dataclass as one JSON-serializable mapping."""
        return cast("dict[str, JsonValue]", asdict(self))


@dataclass
class _CapturedRequestState:
    """Per-invocation request state populated by patched provider helpers."""

    request_kwargs: dict[str, Any] | None = None
    system_prompt: object = None


_llm_request_logging_context: ContextVar[LLMRequestLoggingContext | None] = ContextVar(
    "mindroom_llm_request_logging_context",
    default=None,
)


def resolve_llm_request_log_dir(
    *,
    runtime_paths: RuntimePaths,
    configured_log_dir: str | None,
) -> Path:
    """Resolve the active log directory for LLM request JSONL files."""
    if configured_log_dir:
        return resolve_config_relative_path(configured_log_dir, runtime_paths)
    return runtime_paths.storage_root / "logs" / "llm_requests"


def _normalize_secret_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _is_secret_key(value: object) -> bool:
    normalized = _normalize_secret_key(value)
    compact = normalized.replace("_", "")
    for secret_key in _SECRET_KEYS:
        secret_compact = secret_key.replace("_", "")
        if (
            normalized == secret_key
            or normalized.endswith(f"_{secret_key}")
            or compact == secret_compact
            or compact.endswith(secret_compact)
        ):
            return True
    return False


def _safe_repr(value: object) -> str:
    try:
        return repr(value)
    except BaseException:
        return f"<unrepresentable: {type(value).__name__}>"


def _dump_pydantic_model(value: BaseModel) -> JsonValue:
    try:
        return _json_safe_value(value.model_dump(exclude_none=True))
    except Exception:
        return _safe_repr(value)


def _dump_dataclass(value: object) -> JsonValue:
    try:
        return _json_safe_value(asdict(cast("Any", value)))
    except Exception:
        return _safe_repr(value)


def _dump_model_like_object(value: object) -> JsonValue | None:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_safe_value(model_dump(exclude_none=True))
        except Exception:
            return _safe_repr(value)
    return None


def _dump_object_dict(value: object) -> JsonValue | None:
    dict_value = getattr(value, "__dict__", None)
    if isinstance(dict_value, dict) and dict_value:
        return _json_safe_value(dict(dict_value))
    return None


def _sanitize_mapping(mapping: dict[object, object]) -> dict[str, JsonValue]:
    sanitized: dict[str, JsonValue] = {}
    for key, item in mapping.items():
        key_text = str(key)
        sanitized[key_text] = _REDACTED if _is_secret_key(key_text) else _json_safe_value(item)
    return sanitized


def _json_safe_value(value: object) -> JsonValue:
    if value is None or isinstance(value, bool | int | float | str):
        result = cast("JsonValue", value)
    elif isinstance(value, Path):
        result = str(value)
    elif isinstance(value, bytes):
        result = _safe_repr(value)
    elif isinstance(value, BaseModel):
        result = _dump_pydantic_model(value)
    elif is_dataclass(value) and not isinstance(value, type):
        result = _dump_dataclass(value)
    elif isinstance(value, dict):
        result = _sanitize_mapping(cast("dict[object, object]", value))
    elif isinstance(value, list | tuple | set | frozenset):
        result = [_json_safe_value(item) for item in value]
    else:
        result = _dump_model_like_object(value)
        if result is None:
            result = _dump_object_dict(value)
        if result is None:
            result = _safe_repr(value)
    return result


def _repr_fallback(value: object) -> str:
    return _safe_repr(value)


def _extract_system_prompt(
    messages: list[Message],
    captured_system_prompt: object,
    request_kwargs: dict[str, Any],
) -> JsonValue:
    if captured_system_prompt is not None:
        return _json_safe_value(captured_system_prompt)
    if "system" in request_kwargs:
        return _json_safe_value(request_kwargs["system"])
    config = request_kwargs.get("config")
    if isinstance(config, dict) and "system_instruction" in config:
        return _json_safe_value(config["system_instruction"])

    system_fragments: list[JsonValue] = []
    for message in messages:
        if message.role not in {"system", "developer"}:
            continue
        system_fragments.append(_json_safe_value(message.model_dump(exclude_none=True)))
    if len(system_fragments) == 1:
        return system_fragments[0]
    return system_fragments


def _extract_cache_metadata(payload: JsonValue) -> JsonValue:
    if isinstance(payload, dict):
        cache_items: dict[str, JsonValue] = {}
        for key, value in payload.items():
            nested = _extract_cache_metadata(value)
            if "cache" in key.lower():
                cache_items[key] = value
            elif nested not in (None, {}, []):
                cache_items[key] = nested
        return cache_items
    if isinstance(payload, list):
        return [item for item in (_extract_cache_metadata(item) for item in payload) if item not in (None, {}, [])]
    return None


def _messages_payload(messages: list[Message]) -> JsonValue:
    return [_json_safe_value(message.model_dump(exclude_none=True)) for message in messages]


def _build_request_record(
    *,
    model: Model,
    messages: list[Message],
    tools: list[dict[str, Any]] | None,
    captured: _CapturedRequestState,
    default_agent_name: str | None,
    default_model_config_name: str | None,
) -> LLMRequestRecord:
    context = _llm_request_logging_context.get()
    request_kwargs = captured.request_kwargs or {}
    safe_request_kwargs = cast("dict[str, JsonValue]", _json_safe_value(request_kwargs))
    if not isinstance(safe_request_kwargs, dict):
        safe_request_kwargs = {"request_kwargs": safe_request_kwargs}
    system_prompt = _extract_system_prompt(messages, captured.system_prompt, request_kwargs)
    model_parameters = dict(safe_request_kwargs)
    model_parameters.pop("system", None)
    cache_metadata = _extract_cache_metadata(
        {
            "system_prompt": system_prompt,
            "model_parameters": model_parameters,
        },
    )
    return LLMRequestRecord(
        timestamp=datetime.now(UTC).isoformat(),
        agent_name=(context.agent_name if context is not None else None) or default_agent_name,
        session_id=context.session_id if context is not None else None,
        room_id=context.room_id if context is not None else None,
        thread_id=context.thread_id if context is not None else None,
        run_id=context.run_id if context is not None else None,
        provider=model.provider or type(model).__name__,
        model_config_name=(context.model_config_name if context is not None else None) or default_model_config_name,
        model_id=model.id,
        system_prompt=system_prompt,
        messages=_messages_payload(messages),
        tools=_json_safe_value(tools),
        model_parameters=model_parameters,
        cache_metadata=cache_metadata,
    )


@contextmanager
def bind_llm_request_logging_context(
    *,
    session_id: str | None = None,
    room_id: str | None = None,
    thread_id: str | None = None,
    run_id: str | None = None,
    agent_name: str | None = None,
    model_config_name: str | None = None,
) -> Generator[None]:
    """Bind dynamic request metadata for any model calls inside the context."""
    current = _llm_request_logging_context.get()
    context = (
        replace(
            current,
            session_id=session_id if session_id is not None else current.session_id,
            room_id=room_id if room_id is not None else current.room_id,
            thread_id=thread_id if thread_id is not None else current.thread_id,
            run_id=run_id if run_id is not None else current.run_id,
            agent_name=agent_name if agent_name is not None else current.agent_name,
            model_config_name=model_config_name if model_config_name is not None else current.model_config_name,
        )
        if current is not None
        else LLMRequestLoggingContext(
            session_id=session_id,
            room_id=room_id,
            thread_id=thread_id,
            run_id=run_id,
            agent_name=agent_name,
            model_config_name=model_config_name,
        )
    )
    token = _llm_request_logging_context.set(context)
    try:
        yield
    finally:
        _llm_request_logging_context.reset(token)


def _get_daily_log_path(log_dir: Path, *, now: datetime | None = None) -> Path:
    current_time = now or datetime.now(UTC)
    return log_dir / f"llm_requests_{current_time.date().isoformat()}.jsonl"


def _append_jsonl_line(log_path: Path, line: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.write("\n")


async def log_llm_request(record: LLMRequestRecord, *, log_dir: Path, now: datetime | None = None) -> None:
    """Persist one pre-provider LLM request assembly record without blocking the event loop."""
    log_path = _get_daily_log_path(log_dir, now=now)
    try:
        line = json.dumps(record.as_dict(), ensure_ascii=False, default=_repr_fallback)
        async with _WRITE_LOCK:
            await asyncio.to_thread(_append_jsonl_line, log_path, line)
    except Exception:
        logger.warning("Failed to persist LLM request log entry", log_path=str(log_path), exc_info=True)


def _set_captured_request_state(
    captured: _CapturedRequestState,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> None:
    if args:
        captured.system_prompt = args[0]
    elif "system_message" in kwargs:
        captured.system_prompt = kwargs["system_message"]


def _install_prepare_request_capture(
    *,
    model_obj: object,
    model_dict: dict[str, Any],
    captured: _CapturedRequestState,
) -> None:
    original_prepare_request_kwargs = cast("Any", model_obj)._prepare_request_kwargs

    def _prepare_request_kwargs_with_capture(*args: object, **kwargs: object) -> dict[str, Any]:
        _set_captured_request_state(captured, args, kwargs)
        result = cast("dict[str, Any]", original_prepare_request_kwargs(*args, **kwargs))
        captured.request_kwargs = dict(result)
        return result

    model_dict["_prepare_request_kwargs"] = _prepare_request_kwargs_with_capture


def _install_get_request_params_capture(
    *,
    model_obj: object,
    model_dict: dict[str, Any],
    captured: _CapturedRequestState,
) -> None:
    original_get_request_params = cast("Any", model_obj).get_request_params

    def _get_request_params_with_capture(*args: object, **kwargs: object) -> dict[str, Any]:
        _set_captured_request_state(captured, args, kwargs)
        result = cast("dict[str, Any]", original_get_request_params(*args, **kwargs))
        captured.request_kwargs = dict(result)
        return result

    model_dict["get_request_params"] = _get_request_params_with_capture


def _install_request_capture(
    *,
    model: Model,
    model_obj: object,
    model_dict: dict[str, Any],
    captured: _CapturedRequestState,
) -> None:
    if "_prepare_request_kwargs" in dir(model):
        _install_prepare_request_capture(model_obj=model_obj, model_dict=model_dict, captured=captured)
        return
    if "get_request_params" in dir(model):
        _install_get_request_params_capture(model_obj=model_obj, model_dict=model_dict, captured=captured)


def install_llm_request_logging_hooks(
    model: Model,
    *,
    log_dir: Path,
    default_agent_name: str | None,
    default_model_config_name: str | None,
) -> None:
    """Patch one model instance to emit structured request logs."""
    try:
        model_any = cast("Any", model)
        model_dict = vars(model)
        original_ainvoke = model.ainvoke
        original_ainvoke_stream = model.ainvoke_stream
    except (AttributeError, TypeError):
        return
    if model_dict.get(_LLM_REQUEST_LOGGING_HOOK_ATTR) is True:
        return
    model_dict[_LLM_REQUEST_LOGGING_HOOK_ATTR] = True

    captured = _CapturedRequestState()
    _install_request_capture(
        model=model,
        model_obj=model_any,
        model_dict=model_dict,
        captured=captured,
    )

    async def _log_from_capture(messages: list[Message], tools: list[dict[str, Any]] | None) -> None:
        record = _build_request_record(
            model=model,
            messages=messages,
            tools=tools,
            captured=captured,
            default_agent_name=default_agent_name,
            default_model_config_name=default_model_config_name,
        )
        await log_llm_request(record, log_dir=log_dir)

    async def _ainvoke_with_logging(
        messages: list[Message],
        assistant_message: Message,
        response_format: dict[str, Any] | type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        run_response: object | None = None,
        compress_tool_results: bool = False,
        **kwargs: object,
    ) -> object:
        captured.request_kwargs = None
        captured.system_prompt = None
        try:
            return await original_ainvoke(
                messages,
                assistant_message,
                response_format=response_format,
                tools=tools,
                tool_choice=tool_choice,
                run_response=run_response,
                compress_tool_results=compress_tool_results,
                **kwargs,
            )
        finally:
            await _log_from_capture(messages, tools)

    async def _ainvoke_stream_with_logging(
        messages: list[Message],
        assistant_message: Message,
        response_format: dict[str, Any] | type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        run_response: object | None = None,
        compress_tool_results: bool = False,
        **kwargs: object,
    ) -> AsyncIterator[object]:
        captured.request_kwargs = None
        captured.system_prompt = None
        logged = False
        try:
            async for chunk in original_ainvoke_stream(
                messages,
                assistant_message,
                response_format=response_format,
                tools=tools,
                tool_choice=tool_choice,
                run_response=run_response,
                compress_tool_results=compress_tool_results,
                **kwargs,
            ):
                if not logged and captured.request_kwargs is not None:
                    await _log_from_capture(messages, tools)
                    logged = True
                yield chunk
        finally:
            if not logged:
                await _log_from_capture(messages, tools)

    model_dict["ainvoke"] = _ainvoke_with_logging
    model_dict["ainvoke_stream"] = _ainvoke_stream_with_logging
