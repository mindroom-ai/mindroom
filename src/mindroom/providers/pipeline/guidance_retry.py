"""Request once more, with guidance, after the model wrote a tool call the provider could not parse.

Agno answered Gemini's MALFORMED_FUNCTION_CALL by appending a temporary user message with its guidance text and
requesting again; this stage does the same for any route that fails with ``MALFORMED_TOOL_CALL`` before visible
output. The guidance travels on the retried request only, so it never reaches the conversation the caller stores.
A second malformed call fails the request.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from mindroom.llm.errors import MALFORMED_TOOL_CALL, ProviderError
from mindroom.llm.messages import Message
from mindroom.llm.provider import ReportedUsage
from mindroom.logging_config import get_logger
from mindroom.providers.pipeline.wrapped import is_visible_output, with_carried_usage
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.llm.provider import ModelProvider, ModelRequest, StreamEvent

__all__ = ["MALFORMED_FUNCTION_CALL_GUIDANCE", "guidance_retry_stream"]

logger = get_logger(__name__)

# The guidance Agno's retry loop sent once after a malformed function call; this stage sends the same text.
MALFORMED_FUNCTION_CALL_GUIDANCE = """The previous function call was malformed. Please try again with a valid function call.

Guidelines:
- Generate the function call JSON directly, do not generate code
- Use the function name exactly as defined (no namespace prefixes like 'default_api.')
- Ensure all required parameters are provided with correct types
"""


async def guidance_retry_stream(inner: ModelProvider, request: ModelRequest) -> AsyncIterator[StreamEvent]:
    """Stream ``request``, resending it once with the malformed-call guidance after a malformed tool call."""
    visible = False
    usage = ReportedUsage()
    try:
        events = inner.stream(request)
        async with closing_async_stream(events):
            async for event in events:
                usage.observe(event)
                visible = visible or is_visible_output(event)
                yield event
    except ProviderError as error:
        if visible or error.code != MALFORMED_TOOL_CALL:
            raise
    else:
        return
    identity = inner.identity
    logger.warning(
        "Retrying model request with malformed tool-call guidance",
        provider=identity.provider,
        model_id=identity.id,
    )
    if (report := usage.settle()) is not None:
        yield report
    guidance = Message(role="user", content=MALFORMED_FUNCTION_CALL_GUIDANCE)
    events = inner.stream(replace(request, messages=(*request.messages, guidance)))
    async with closing_async_stream(events):
        async for event in events:
            yield with_carried_usage(event, usage.settled)
