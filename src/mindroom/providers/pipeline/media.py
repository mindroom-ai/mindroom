"""Send a request without inline media its route cannot take, and retry once without the rest on rejection.

Media a route never accepts (``Capabilities.omitted_media``) and media a route rejected earlier in this
process are replaced up front by a note telling the model the attachment was not inspected. When the
provider then rejects the request before the attempt committed (no visible output and no hosted tool that acts),
the remaining media goes the same way and the request is sent once more. A rejection that names the missing
capability is remembered for the route.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from mindroom.llm.errors import MALFORMED_TOOL_CALL, SAFEGUARD_REFUSAL, ContextWindowExceeded, ProviderError
from mindroom.logging_config import get_logger
from mindroom.providers.pipeline.wrapped import Retry, attempts
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import MediaKind, ModelProvider, ModelRequest, StreamEvent

__all__ = ["FALLBACK_MARKER", "media_fallback_stream"]

logger = get_logger(__name__)

FALLBACK_MARKER = "[Inline media unavailable for this model]"
_UNSUPPORTED_VIEW_MESSAGE = (
    "The active model rejected the viewed image input; the retained attachment is still available."
)
# A retried media-free request must not repeat a failure the media cannot have caused.
_NOT_MEDIA_RELATED_CODES = frozenset({SAFEGUARD_REFUSAL, MALFORMED_TOOL_CALL})
_PAYLOAD_TOO_LARGE_STATUS = 413
_SERVER_ERROR_STATUS = 500
# Rejections naming one input's size or type prove only that this input failed, not the whole media kind.
_INPUT_SPECIFIC_ERROR_MARKERS = ("exceed", "too large", "media_type", "mime type", "mime_type")
# Only a rejection that names the missing capability may change later requests on the whole route.
_CAPABILITY_GAP_ERROR = re.compile(
    r"(?:does not|doesn't) support (?:(?:image|audio|video|file|pdf|document) inputs?|images|vision)\b"
    r"|(?:image|audio|video|file|pdf|document) inputs? (?:is|are) not supported"
    r"|is not a multimodal model"
    r"|is only supported by certain models"
    r"|no endpoints found that support (?:image|audio|video|file|pdf|document)",
)


@dataclass(frozen=True, slots=True)
class _Route:
    provider: str
    model_id: str
    endpoint: str | None


# Learned limits live for the process only; a provider may add a capability at any time.
_LEARNED_UNSUPPORTED: dict[_Route, set[MediaKind]] = {}


def _media_kinds(messages: Sequence[Message]) -> frozenset[MediaKind]:
    kinds: set[MediaKind] = set()
    for message in messages:
        if message.audio:
            kinds.add("audio")
        if message.images:
            kinds.add("image")
        if message.files:
            kinds.add("file")
        if message.videos:
            kinds.add("video")
    return frozenset(kinds)


def _mark_view_unsupported(content: str | list[Any] | None) -> str | list[Any] | None:
    """Turn one ready ``get_attachment(view=True)`` receipt into an explicit unsupported result."""
    if not isinstance(content, str):
        return content
    try:
        receipt = json.loads(content)
    except (TypeError, ValueError):
        return content
    if not isinstance(receipt, dict) or receipt.get("view_status") != "ready":
        return content
    receipt["view_status"] = "unsupported"
    receipt["message"] = _UNSUPPORTED_VIEW_MESSAGE
    return json.dumps(receipt, sort_keys=True)


def _with_note(content: str | list[Any] | None, note: str) -> str | list[Any]:
    if isinstance(content, list):
        return [*content, {"type": "text", "text": note}]
    return f"{content}\n\n{note}" if content else note


def _without_media(messages: Sequence[Message], removed: frozenset[MediaKind], fallback_prompt: str) -> list[Message]:
    """Return copies without the removed kinds; each message that lost media carries the fallback note."""
    if not removed:
        return list(messages)
    note = f"{FALLBACK_MARKER}\n{fallback_prompt}"
    stripped: list[Message] = []
    for message in messages:
        content = message.content
        if "image" in removed:
            # Every ready view receipt, also one whose image follows its tool results as a separate message.
            content = _mark_view_unsupported(content)
        lost_media = bool(removed & _media_kinds([message]))
        stripped.append(
            replace(
                message,
                audio=None if "audio" in removed else message.audio,
                images=None if "image" in removed else message.images,
                files=None if "file" in removed else message.files,
                videos=None if "video" in removed else message.videos,
                content=_with_note(content, note) if lost_media else content,
            ),
        )
    return stripped


def _names_capability_gap(error: ProviderError, kinds: frozenset[MediaKind]) -> bool:
    """Return whether a media-free success isolates one kind the provider says the model lacks."""
    if len(kinds) != 1 or isinstance(error, ContextWindowExceeded):
        return False
    status = error.status_code
    # A 413 or a server error says nothing about the model; rate limits are retryable, so they never reach this check.
    if status is not None and (status == _PAYLOAD_TOO_LARGE_STATUS or status >= _SERVER_ERROR_STATUS):
        return False
    text = str(error).lower()
    if f"error code: {_PAYLOAD_TOO_LARGE_STATUS}" in text or any(
        marker in text for marker in _INPUT_SPECIFIC_ERROR_MARKERS
    ):
        return False
    return _CAPABILITY_GAP_ERROR.search(text) is not None


async def media_fallback_stream(
    inner: ModelProvider,
    request: ModelRequest,
    *,
    fallback_prompt: str,
) -> AsyncIterator[StreamEvent]:
    """Stream ``request`` with unsupported media replaced, retrying once media-free after a rejection."""
    identity = inner.identity
    route = _Route(provider=identity.provider.lower(), model_id=identity.id, endpoint=identity.endpoint)
    present = _media_kinds(request.messages)
    known = (frozenset(_LEARNED_UNSUPPORTED.get(route, ())) | inner.capabilities.omitted_media) & present
    remaining = present - known
    failure: ProviderError | None = None

    def without_remaining_media(error: ProviderError) -> Retry | None:
        nonlocal failure
        if failure is not None or not remaining or error.retryable or error.code in _NOT_MEDIA_RELATED_CODES:
            return None
        failure = error
        logger.warning(
            "Retrying model request without inline media",
            provider=identity.provider,
            model_id=identity.id,
            status_code=error.status_code,
            error=redact_sensitive_text(str(error), max_length=500),
        )
        return Retry(replace(request, messages=_without_media(request.messages, present, fallback_prompt)))

    first = replace(request, messages=_without_media(request.messages, known, fallback_prompt))
    events = attempts(inner, first, without_remaining_media)
    async with closing_async_stream(events):
        async for event in events:
            yield event
    if failure is not None and _names_capability_gap(failure, remaining):
        _LEARNED_UNSUPPORTED.setdefault(route, set()).update(remaining)
