"""The per-request log context that callers bind around one agent or team run.

The request pipeline (``providers/pipeline/request_log.py``) reads the bound context when it emits usage
telemetry and the opt-in request log.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.constants import MATRIX_SOURCE_EVENT_IDS_METADATA_KEY, MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY
from mindroom.llm.request_context import bind_llm_request_log_context, current_llm_request_log_context
from mindroom.tool_system.context_bound_streams import context_bound_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

__all__ = [
    "bind_llm_request_log_context",
    "build_llm_request_log_context",
    "current_llm_request_log_context",
    "stream_with_llm_request_log_context",
]


def _normalized_string_list(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for value in values:
        if isinstance(value, str) and value and value not in normalized:
            normalized.append(value)
    return normalized


def build_llm_request_log_context(
    *,
    agent_id: str,
    session_id: str,
    room_id: str | None,
    thread_id: str | None,
    reply_to_event_id: str | None,
    requester_id: str | None,
    correlation_id: str,
    prompt: str,
    model_prompt: str | None,
    full_prompt: str,
    metadata: dict[str, object] | None,
) -> dict[str, object]:
    """Build explicit per-request log context for one provider call."""
    context: dict[str, object] = {
        "agent_id": agent_id,
        "session_id": session_id,
        "correlation_id": correlation_id,
        "current_turn_prompt": prompt,
        "full_prompt": full_prompt,
    }
    if room_id:
        context["room_id"] = room_id
    if thread_id:
        context["thread_id"] = thread_id
    if reply_to_event_id:
        context["reply_to_event_id"] = reply_to_event_id
    if requester_id is not None:
        context["requester_id"] = requester_id
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


def stream_with_llm_request_log_context[StreamEventT](
    stream_generator: AsyncIterator[StreamEventT],
    *,
    request_context: dict[str, object],
) -> AsyncIterator[StreamEventT]:
    """Advance one async stream with request-log context bound per item pull."""
    return context_bound_async_stream(
        context_factory=lambda: bind_llm_request_log_context(**request_context),
        stream_factory=stream_generator.__aiter__,
    )
