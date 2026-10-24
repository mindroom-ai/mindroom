"""Request once more, with guidance, after the model wrote a tool call the provider could not parse.

A request that fails with ``MALFORMED_TOOL_CALL`` before the attempt committed is sent once more with a temporary
user message telling the model how to write the call. The guidance travels on the retried request only, so it never
reaches the conversation the caller stores. A second malformed call fails the request.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from mindroom.llm.errors import MALFORMED_TOOL_CALL
from mindroom.llm.messages import Message
from mindroom.logging_config import get_logger
from mindroom.providers.pipeline.wrapped import Retry, attempts

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.llm.errors import ProviderError
    from mindroom.llm.provider import ModelProvider, ModelRequest, StreamEvent

__all__ = ["MALFORMED_FUNCTION_CALL_GUIDANCE", "guidance_retry_stream"]

logger = get_logger(__name__)

# Model-visible text, kept as main behaves.
MALFORMED_FUNCTION_CALL_GUIDANCE = """The previous function call was malformed. Please try again with a valid function call.

Guidelines:
- Generate the function call JSON directly, do not generate code
- Use the function name exactly as defined (no namespace prefixes like 'default_api.')
- Ensure all required parameters are provided with correct types
"""


def guidance_retry_stream(inner: ModelProvider, request: ModelRequest) -> AsyncIterator[StreamEvent]:
    """Stream ``request``, resending it once with the malformed-call guidance after a malformed tool call."""
    retried = False

    def with_guidance(error: ProviderError) -> Retry | None:
        nonlocal retried
        if retried or error.code != MALFORMED_TOOL_CALL:
            return None
        retried = True
        identity = inner.identity
        logger.warning(
            "Retrying model request with malformed tool-call guidance",
            provider=identity.provider,
            model_id=identity.id,
        )
        guidance = Message(role="user", content=MALFORMED_FUNCTION_CALL_GUIDANCE)
        return Retry(replace(request, messages=(*request.messages, guidance)))

    return attempts(inner, request, with_guidance)
