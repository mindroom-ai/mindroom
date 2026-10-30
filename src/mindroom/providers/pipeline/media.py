"""Send a request without inline media its route cannot take, and retry without what the provider rejects.

Media a route never accepts (``Capabilities.omitted_media``) and media a route rejected earlier in this
process are replaced up front by a note telling the model the attachment was not inspected. When the
provider then rejects the request before the attempt committed (no visible output and no hosted tool that acts),
the request is sent again without media. A rejection that names some of the request's media items
(``ProviderError.rejected_media``) is retried without only those, with a note that names them; when it names none or
all of them, or the provider rejects that retry too, the request is sent once more without any of its media, exactly
as it would have been without the named retry. A rejection that names the missing capability is remembered for the
route, once a request without any of the media succeeded.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any

from mindroom.llm.errors import (
    MALFORMED_TOOL_CALL,
    QUOTA_EXHAUSTED,
    SAFEGUARD_REFUSAL,
    ContextWindowExceeded,
    InvalidRequest,
    ProviderError,
)
from mindroom.llm.media import VIEWED_IMAGE_ID_PREFIX, File
from mindroom.llm.messages import media_items
from mindroom.logging_config import get_logger
from mindroom.providers.pipeline.wrapped import Retry, attempts
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from mindroom.llm.media import Audio, Image, Video
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import MediaKind, ModelProvider, ModelRequest, StreamEvent

    type _Media = Image | Audio | Video | File

__all__ = ["FALLBACK_MARKER", "media_fallback_stream"]

logger = get_logger(__name__)

FALLBACK_MARKER = "[Inline media unavailable for this model]"
_UNSUPPORTED_VIEW_MESSAGE = (
    "The active model rejected the viewed image input; the retained attachment is still available."
)
# The ids of stored attachments, which ``get_attachment`` takes, so the note names them beside their labels.
_ATTACHMENT_ID_PREFIX = "att_"
_IMAGES: frozenset[MediaKind] = frozenset({"image"})
# A retried media-free request must not repeat a failure the media cannot have caused.
_NOT_MEDIA_RELATED_CODES = frozenset({SAFEGUARD_REFUSAL, MALFORMED_TOOL_CALL, QUOTA_EXHAUSTED})
_PAYLOAD_TOO_LARGE_STATUS = 413
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
    return frozenset(kind for message in messages for kind, _item in media_items(message))


def _label(kind: MediaKind, item: _Media) -> str:
    """Return how the note names a removed item: its file name, else its kind, with the id ``get_attachment`` takes."""
    label = (item.filename or item.name if isinstance(item, File) else None) or kind
    return f"{label} ({item.id})" if item.id.startswith(_ATTACHMENT_ID_PREFIX) else label


def _note(named: Sequence[tuple[MediaKind, _Media]], fallback_prompt: str) -> str:
    """Return the fallback note, which names the items removed by id when there are any."""
    if not named:
        return f"{FALLBACK_MARKER}\n{fallback_prompt}"
    labels = ", ".join(_label(kind, item) for kind, item in named)
    return f"{FALLBACK_MARKER}\nNot sent: {labels}.\n{fallback_prompt}"


def _kept[T: (Image, Audio, Video, File)](
    items: Sequence[T] | None,
    kind: MediaKind,
    removed: frozenset[MediaKind],
    ids: frozenset[str],
) -> Sequence[T] | None:
    """Return the items of one kind that stay: none of a removed kind, else those whose id is not in ``ids``."""
    if kind in removed:
        return None
    if not items or not ids:
        return items
    return [item for item in items if item.id not in ids] or None


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


def _without_media(
    messages: Sequence[Message],
    removed: frozenset[MediaKind],
    ids: frozenset[str],
    fallback_prompt: str,
) -> list[Message]:
    """Return copies without the removed kinds and the items whose id is in ``ids``.

    Each message that lost media carries the fallback note, which names the items ``ids`` removed.
    """
    if not removed and not ids:
        return list(messages)
    stripped: list[Message] = []
    for message in messages:
        content = message.content
        if "image" in removed:
            # Every ready view receipt, also one whose image follows its tool results as a separate message.
            content = _mark_view_unsupported(content)
        named = [(kind, item) for kind, item in media_items(message) if item.id in ids]
        lost_media = bool(removed & _media_kinds([message])) or bool(named)
        stripped.append(
            replace(
                message,
                audio=_kept(message.audio, "audio", removed, ids),
                images=_kept(message.images, "image", removed, ids),
                files=_kept(message.files, "file", removed, ids),
                videos=_kept(message.videos, "video", removed, ids),
                content=_with_note(content, _note(named, fallback_prompt)) if lost_media else content,
            ),
        )
    return stripped


def _names_capability_gap(error: ProviderError, kinds: frozenset[MediaKind]) -> bool:
    """Return whether a media-free success isolates one kind the provider says the model lacks."""
    if len(kinds) != 1 or isinstance(error, ContextWindowExceeded):
        return False
    # A 413 says nothing about the model; rate limits and server errors are retryable, so they never reach this check.
    if error.status_code == _PAYLOAD_TOO_LARGE_STATUS:
        return False
    text = str(error).lower()
    if any(marker in text for marker in _INPUT_SPECIFIC_ERROR_MARKERS):
        return False
    return _CAPABILITY_GAP_ERROR.search(text) is not None


def _selective_removal(
    rejected: frozenset[str],
    sent: dict[str, MediaKind],
    known: frozenset[MediaKind],
) -> tuple[frozenset[MediaKind], frozenset[str]] | None:
    """Return the kinds and ids a retry without only the ``rejected`` items removes, or None when it would keep nothing.

    ``sent`` maps the id of each item the first request sent to its kind. A viewed image cannot be told apart from the
    other images by its tool receipt, so rejecting one removes every image and marks the receipts.
    """
    ids = rejected & sent.keys()
    if not ids:
        return None
    viewed = any(item.startswith(VIEWED_IMAGE_ID_PREFIX) for item in ids)
    removed = known | _IMAGES if viewed else known
    if all(item in ids or kind in removed for item, kind in sent.items()):
        return None
    return removed, ids


async def media_fallback_stream(
    inner: ModelProvider,
    request: ModelRequest,
    *,
    fallback_prompt: str,
) -> AsyncIterator[StreamEvent]:
    """Stream ``request`` with unsupported media replaced, retrying without what a rejection names, then media-free."""
    identity = inner.identity
    route = _Route(provider=identity.provider.lower(), model_id=identity.id, endpoint=identity.endpoint)
    present = _media_kinds(request.messages)
    known = (frozenset(_LEARNED_UNSUPPORTED.get(route, ())) | inner.capabilities.omitted_media) & present
    remaining = present - known
    first = replace(request, messages=_without_media(request.messages, known, frozenset(), fallback_prompt))
    sent = {item.id: kind for message in first.messages for kind, item in media_items(message)}
    failure: ProviderError | None = None
    sent_broad = False

    def without_rejected_media(error: ProviderError) -> Retry | None:
        nonlocal failure, sent_broad
        if sent_broad or not remaining or error.retryable or error.code in _NOT_MEDIA_RELATED_CODES:
            return None
        log = partial(
            logger.warning,
            provider=identity.provider,
            model_id=identity.id,
            status_code=error.status_code,
            error=redact_sensitive_text(str(error), max_length=500),
        )
        if failure is None:
            failure = error
            # Only a rejected request body can name its parts; a request too large for the window shrinks as a whole.
            named = error.rejected_media if isinstance(error, InvalidRequest) else frozenset()
            if (selective := _selective_removal(named, sent, known)) is not None:
                removed, ids = selective
                log("Retrying model request without the inline media its failure names", rejected_media=len(ids))
                return Retry(replace(request, messages=_without_media(request.messages, removed, ids, fallback_prompt)))
        sent_broad = True
        log("Retrying model request without inline media")
        return Retry(replace(request, messages=_without_media(request.messages, present, frozenset(), fallback_prompt)))

    events = attempts(inner, first, without_rejected_media)
    async with closing_async_stream(events):
        async for event in events:
            yield event
    # Only a request without any of the media isolates what the model lacks; one without some of it proves nothing.
    if sent_broad and failure is not None and _names_capability_gap(failure, remaining):
        _LEARNED_UNSUPPORTED.setdefault(route, set()).update(remaining)
