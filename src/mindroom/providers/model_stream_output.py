"""Shared output boundary for provider stream retries."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agno.models.response import ModelResponse


def has_meaningful_stream_output(response: ModelResponse) -> bool:
    """Return whether retrying one streamed delta would duplicate retained state.

    Role/event bookkeeping alone is safe to repeat. Downstream consumers
    accumulate payload fields, including provider data and tool calls.
    """
    return bool(
        response.content
        or response.parsed
        or response.audio
        or response.images
        or response.videos
        or response.audios
        or response.files
        or response.tool_calls
        or response.tool_executions
        or response.provider_data
        or response.reasoning_content
        or response.redacted_reasoning_content
        or response.citations
        or response.response_usage
        or response.extra
        or response.updated_session_state,
    )
