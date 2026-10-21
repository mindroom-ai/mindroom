"""End a request the model's safeguards refused with a typed error instead of a finished turn.

Adapters report a turn's usage before completing it, so the refused request's usage reaches the caller and the
request log first. The error is final: no stage retries the request or resends it without its media.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.llm.errors import ModelSafeguardRefusal
from mindroom.llm.provider import TurnCompleted
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.llm.provider import ModelProvider, ModelRequest, StreamEvent

__all__ = ["refusal_stream"]


async def refusal_stream(inner: ModelProvider, request: ModelRequest) -> AsyncIterator[StreamEvent]:
    """Stream ``request``, raising ``ModelSafeguardRefusal`` in place of a turn that completed as a refusal."""
    events = inner.stream(request)
    async with closing_async_stream(events):
        async for event in events:
            if isinstance(event, TurnCompleted) and event.stop_reason == "refusal":
                raise ModelSafeguardRefusal(provider=inner.identity.provider_name)
            yield event
