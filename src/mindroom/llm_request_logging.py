"""Lightweight opt-in LLM request logging."""

from __future__ import annotations

import asyncio
import json
from dataclasses import fields, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agno.models.message import Message

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

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


def _daily_log_path(log_dir: str | None, default_log_dir: Path, now: datetime) -> Path:
    base_dir = Path(log_dir) if log_dir else default_log_dir
    return base_dir / f"llm-requests-{now.date().isoformat()}.jsonl"


def _system_prompt(messages: Sequence[Message], model: Model) -> str:
    for message in messages:
        if message.role == "system":
            return message.get_content_string()[:500]
    return (model.system_prompt or "")[:500]


def _model_params(model: Model) -> dict[str, Any]:
    if not is_dataclass(model):
        return {}
    payload: dict[str, Any] = {}
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


def _write_jsonl_line(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload))
        handle.write("\n")


def _request_messages(value: object) -> list[Message] | None:
    if isinstance(value, list) and all(isinstance(message, Message) for message in value):
        return cast("list[Message]", value)
    return None


def _request_tools(value: object) -> list[dict[str, Any]] | None:
    if isinstance(value, list) and all(isinstance(tool, dict) for tool in value):
        return cast("list[dict[str, Any]]", value)
    return None


async def write_llm_request_log(
    *,
    model: Model,
    agent_name: str,
    messages: Sequence[Message],
    tools: list[dict[str, Any]] | None,
    log_dir: str | None,
    default_log_dir: Path,
) -> None:
    """Persist one request-summary record for an LLM invocation."""
    now = datetime.now().astimezone()
    await asyncio.to_thread(
        _write_jsonl_line,
        _daily_log_path(log_dir, default_log_dir, now),
        {
            "timestamp": now.isoformat(),
            "agent_name": agent_name,
            "model_id": model.id,
            "system_prompt": _system_prompt(messages, model),
            "message_count": len(messages),
            "tool_count": len(tools or []),
            "model_params": _model_params(model),
        },
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

    async def _logged_ainvoke(*args: object, **kwargs: object) -> ModelResponse:
        messages = _request_messages(kwargs.get("messages"))
        if messages is not None:
            await write_llm_request_log(
                model=model,
                agent_name=agent_name,
                messages=messages,
                tools=_request_tools(kwargs.get("tools")),
                log_dir=debug_config.llm_request_log_dir,
                default_log_dir=default_log_dir,
            )
        return await original_ainvoke(*args, **kwargs)

    async def _logged_ainvoke_stream(*args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
        messages = _request_messages(kwargs.get("messages"))
        if messages is not None:
            await write_llm_request_log(
                model=model,
                agent_name=agent_name,
                messages=messages,
                tools=_request_tools(kwargs.get("tools")),
                log_dir=debug_config.llm_request_log_dir,
                default_log_dir=default_log_dir,
            )
        async for chunk in original_ainvoke_stream(*args, **kwargs):
            yield chunk

    model_dict["ainvoke"] = _logged_ainvoke
    model_dict["ainvoke_stream"] = _logged_ainvoke_stream
    model_dict[_INSTALLED_ATTR] = True
