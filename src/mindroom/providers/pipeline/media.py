"""Send a request without inline media its route cannot take, and retry without what the provider rejects.

Media a route never accepts (``Capabilities.omitted_media``) and media a route rejected earlier in this
process are replaced up front by a note telling the model the attachment was not inspected. On routes that
accept audio, inline audio on any message followed by an assistant message is also replaced up front by a
byte-stable note so later requests of a turn do not resend audio the model already heard. Routes that earlier
accepted transcoded WebP after an image format/MIME rejection also transcode local WebP images to PNG/JPEG up
front. When the provider then rejects the request before the attempt committed (no visible output and no hosted
tool that acts), an image format/MIME rejection first retries once after transcoding local WebP images to
PNG/JPEG; if that does not apply or still fails, the request is sent again without the rejected media.
Item-specific rejections (``ProviderError.rejected_media``) remove only those items where possible; otherwise
an error naming media kinds removes only those kinds present. Untyped rejections and failed narrowed retries
use the all-media-free backstop. Explicitly named unsupported kinds are remembered for the route only after a
successful retry sends no items of those kinds, and WebP incompatibility is remembered only after a transcoded
retry succeeds without stripping images. Input size/type errors never teach route media-kind capabilities, nor
do failed, cancelled, or partially visible attempts.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import warnings
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mindroom.llm.errors import (
    ENCRYPTED_REPLAY_REJECTED,
    MALFORMED_TOOL_CALL,
    QUOTA_EXHAUSTED,
    SAFEGUARD_REFUSAL,
    ContextWindowExceeded,
    InvalidRequest,
    ProviderError,
)
from mindroom.llm.media import IMAGE_HEADER_BYTES, VIEWED_IMAGE_ID_PREFIX, File, image_format, rendered_label
from mindroom.llm.messages import media_items
from mindroom.logging_config import get_logger
from mindroom.providers.pipeline.wrapped import RequestProjection, Retry, attempts
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

    from mindroom.llm.media import Audio, Image, Video
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import MediaKind, ModelProvider, ModelRequest, StreamEvent

    type _Media = Image | Audio | Video | File

__all__ = ["ANSWERED_AUDIO_MARKER", "FALLBACK_MARKER", "media_fallback_projection", "media_fallback_stream"]

logger = get_logger(__name__)

FALLBACK_MARKER = "[Inline media unavailable for this model]"
ANSWERED_AUDIO_MARKER = "[Earlier inline audio omitted]"
_ANSWERED_AUDIO_PROMPT = (
    "Already sent in an earlier request; call get_attachment(attachment_id, view=True) to hear it again if needed."
)
_UNSUPPORTED_VIEW_MESSAGE = (
    "The active model rejected the viewed image input; the retained attachment is still available."
)
# The ids of stored attachments, which ``get_attachment`` takes, so the note names them beside their labels.
_ATTACHMENT_ID_PREFIX = "att_"
_IMAGES: frozenset[MediaKind] = frozenset({"image"})
# A retried media-free request must not repeat a failure the media cannot have caused.
_NOT_MEDIA_RELATED_CODES = frozenset(
    {SAFEGUARD_REFUSAL, MALFORMED_TOOL_CALL, QUOTA_EXHAUSTED, ENCRYPTED_REPLAY_REJECTED},
)
_PAYLOAD_TOO_LARGE_STATUS = 413
# Provider errors naming a capability are short; capping the scan keeps the list patterns cheap on huge errors.
_MAX_SCANNED_ERROR_CHARS = 2000
# Rejections naming one input's size or type prove only that this input failed, not the whole media kind.
_INPUT_SPECIFIC_ERROR_MARKERS = ("exceed", "too large", "size", "format", "media_type", "media type", "mime", "webp")
_MAX_WEBP_SOURCE_BYTES = 20 * 1024 * 1024
_MAX_WEBP_DECODED_PIXELS = 40_000_000
_MAX_WEBP_PNG_BYTES = 5 * 1024 * 1024
_WEBP_TRANSCODE_ERROR_CODES = frozenset({"invalid_image_format", "image_parse_error", "invalid_image"})
_WEBP_TRANSCODE_ERROR_MARKERS = (
    "mime",
    "media_type",
    "media type",
    "format",
    "unsupported image",
    "invalid image",
    "decode image",
    "parse image",
    "png",
    "jpeg",
    "jpg",
)
_MEDIA_KIND_NAMES: dict[MediaKind, str] = {
    "audio": r"audio",
    "image": r"(?:images?|vision)",
    "video": r"videos?",
    "file": r"(?:files?|pdfs?|documents?)",
}
_MEDIA_KIND = rf"(?:{'|'.join(_MEDIA_KIND_NAMES.values())})"
_MEDIA_KIND_LIST = rf"{_MEDIA_KIND}(?:(?:,\s*(?:(?:and|or)\s+)?|\s+(?:and|or)\s+){_MEDIA_KIND})*"
# Bind the unsupported claim to its coordinated kind list, not to supported kinds elsewhere in the error;
# a kind that opens its own "is/are ..." clause ends the list.
_CAPABILITY_GAP_ERRORS = re.compile(
    rf"\b(?:(?:does not|doesn't) support|no endpoints found that support) (?:inputs? )?{_MEDIA_KIND_LIST}\b"
    r"(?!\s+(?:is|are)\b)"
    rf"|\b(?:input )?{_MEDIA_KIND_LIST}(?: inputs?)? (?:is |are )?not supported\b"
    rf"|\b{_MEDIA_KIND_LIST}(?: inputs?)? is only supported by certain models\b",
)


@dataclass(frozen=True, slots=True)
class _Route:
    provider: str
    model_id: str
    endpoint: str | None


# Learned limits live for the process only; a provider may add a capability at any time.
_LEARNED_UNSUPPORTED: dict[_Route, set[MediaKind]] = {}
_LEARNED_WEBP_UNSUPPORTED: set[_Route] = set()


def _media_kinds(messages: Sequence[Message]) -> frozenset[MediaKind]:
    return frozenset(kind for message in messages for kind, _item in media_items(message))


def _label(kind: MediaKind, item: _Media) -> str:
    """Return how the note names a removed item: its file name, else its kind, with the id ``get_attachment`` takes."""
    name = item.filename or item.name if isinstance(item, File) else None
    label = (name and rendered_label(name)) or kind
    return f"{label} ({item.id})" if item.id.startswith(_ATTACHMENT_ID_PREFIX) else label


def _note(
    named: Sequence[tuple[MediaKind, _Media]],
    prompt: str,
    *,
    marker: str = FALLBACK_MARKER,
) -> str:
    """Return a media-omission note, which names the ``named`` items when there are any."""
    if not named:
        return f"{marker}\n{prompt}"
    labels = ", ".join(_label(kind, item) for kind, item in named)
    return f"{marker}\nNot sent: {labels}.\n{prompt}"


def _without_answered_audio(messages: Sequence[Message]) -> list[Message]:
    """Return copies dropping inline audio from messages followed by an assistant message, leaving a stable note."""
    last_assistant = max((index for index, message in enumerate(messages) if message.role == "assistant"), default=-1)
    if last_assistant <= 0 or not any(message.audio for message in messages[:last_assistant]):
        return list(messages)
    return [
        replace(
            message,
            audio=None,
            content=_with_note(
                message.content,
                _note(
                    [("audio", clip) for clip in message.audio],
                    _ANSWERED_AUDIO_PROMPT,
                    marker=ANSWERED_AUDIO_MARKER,
                ),
            ),
        )
        if index < last_assistant and message.audio
        else message
        for index, message in enumerate(messages)
    ]


def _prepared_messages(
    inner: ModelProvider,
    messages: Sequence[Message],
) -> tuple[Sequence[Message], frozenset[MediaKind], frozenset[MediaKind]]:
    """Return ``(base, present, known)`` after dropping answered audio on audio-capable routes."""
    unsupported = _unsupported_kinds(inner)
    base = messages if "audio" in unsupported else _without_answered_audio(messages)
    present = _media_kinds(base)
    return base, present, unsupported & present


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

    Each message that lost media carries the fallback note. The note of a message that lost an item in ``ids`` names
    every item it lost, of a removed kind too, so its list never reads as complete while leaving one out. Any other
    note names none, as the first request and the media-free retry send it, so a message that lost only removed
    kinds reads as it did in the first request.
    """
    if not removed and not ids:
        return list(messages)
    stripped: list[Message] = []
    for message in messages:
        content = message.content
        if "image" in removed:
            # Every ready view receipt, also one whose image follows its tool results as a separate message.
            content = _mark_view_unsupported(content)
        lost = [(kind, item) for kind, item in media_items(message) if kind in removed or item.id in ids]
        named = lost if any(item.id in ids for _kind, item in lost) else []
        stripped.append(
            replace(
                message,
                audio=_kept(message.audio, "audio", removed, ids),
                images=_kept(message.images, "image", removed, ids),
                files=_kept(message.files, "file", removed, ids),
                videos=_kept(message.videos, "video", removed, ids),
                content=_with_note(content, _note(named, fallback_prompt)) if lost else content,
            ),
        )
    return stripped


def _named_capability_gaps(error: ProviderError) -> frozenset[MediaKind]:
    """Return kinds explicitly described as unsupported, ignoring unrelated media mentions."""
    text = str(error).lower()[:_MAX_SCANNED_ERROR_CHARS]
    return frozenset(
        kind
        for clause in _CAPABILITY_GAP_ERRORS.finditer(text)
        for kind, name in _MEDIA_KIND_NAMES.items()
        if re.search(rf"\b{name}\b", clause.group())
    )


def _named_media_kinds(error: ProviderError) -> frozenset[MediaKind]:
    """Prefer explicit unsupported kinds, otherwise narrow even input-specific failures to the kinds they name."""
    text = str(error).lower()
    return _named_capability_gaps(error) or frozenset(
        kind for kind, name in _MEDIA_KIND_NAMES.items() if re.search(rf"\b{name}\b", text)
    )


def _names_capability_gap(error: ProviderError, removed: frozenset[MediaKind]) -> frozenset[MediaKind]:
    """Return explicitly unsupported kinds fully removed by a successful retry, excluding input-specific errors."""
    if isinstance(error, ContextWindowExceeded) or error.status_code == _PAYLOAD_TOO_LARGE_STATUS:
        return frozenset()
    text = str(error).lower()
    if any(marker in text for marker in _INPUT_SPECIFIC_ERROR_MARKERS):
        return frozenset()
    return _named_capability_gaps(error) & removed


def _read_local_webp_bytes(image: Image) -> bytes | None:
    """Return local WebP bytes within the source cap, else None for non-WebP, remote, or unreadable items."""
    if image.content is not None:
        return (
            image.content
            if 0 < len(image.content) <= _MAX_WEBP_SOURCE_BYTES and image_format(image.content) == "webp"
            else None
        )
    if not image.filepath:
        return None
    try:
        with Path(image.filepath).open("rb") as handle:
            header = handle.read(IMAGE_HEADER_BYTES)
            if image_format(header) != "webp" or os.fstat(handle.fileno()).st_size > _MAX_WEBP_SOURCE_BYTES:
                return None
            data = header + handle.read(_MAX_WEBP_SOURCE_BYTES - len(header) + 1)
    except OSError:
        return None
    return data if len(data) <= _MAX_WEBP_SOURCE_BYTES else None


def _transcode_webp_bytes(data: bytes) -> tuple[bytes, str] | None:
    """Safely decode WebP bytes and encode as PNG, or JPEG when opaque and over the inline PNG cap."""
    from PIL import Image as PILImage  # noqa: PLC0415 - keep Pillow out of module import time
    from PIL import ImageOps, UnidentifiedImageError  # noqa: PLC0415

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PILImage.DecompressionBombWarning)
            with PILImage.open(io.BytesIO(data)) as source:
                width, height = source.size
                if source.format != "WEBP" or width * height > _MAX_WEBP_DECODED_PIXELS:
                    return None
                source.seek(0)
                oriented = ImageOps.exif_transpose(source)
                if oriented is None:
                    return None
                if oriented.mode == "CMYK":
                    oriented = oriented.convert("RGB")
                output = io.BytesIO()
                oriented.save(output, format="PNG")
                encoded = output.getvalue()
                if len(encoded) <= _MAX_WEBP_PNG_BYTES or "A" in oriented.getbands() or "transparency" in oriented.info:
                    return encoded, "image/png"
                jpeg_output = io.BytesIO()
                oriented.convert("RGB").save(jpeg_output, format="JPEG", quality=85)
                return jpeg_output.getvalue(), "image/jpeg"
    except (
        OSError,
        ValueError,
        SyntaxError,
        UnidentifiedImageError,
        PILImage.DecompressionBombError,
        PILImage.DecompressionBombWarning,
    ):
        return None


def _transcode_webp_messages(messages: Sequence[Message]) -> list[Message] | None:
    """Transcode local WebP images in ``messages`` to PNG/JPEG; return None when no image was transcoded."""
    rewritten: list[Message] = []
    changed = False
    for message in messages:
        if not message.images:
            rewritten.append(message)
            continue
        replaced = [
            replace(image, content=encoded[0], filepath=None, url=None, mime_type=encoded[1], format=None)
            if (raw := _read_local_webp_bytes(image)) is not None
            and (encoded := _transcode_webp_bytes(raw)) is not None
            else image
            for image in message.images
        ]
        if any(new is not old for old, new in zip(message.images, replaced)):
            changed = True
            rewritten.append(replace(message, images=replaced))
        else:
            rewritten.append(message)
    return rewritten if changed else None


def _is_webp_transcode_candidate(
    error: ProviderError,
    remaining: frozenset[MediaKind],
    sent: dict[str, MediaKind],
) -> bool:
    """Return whether ``error`` looks like an image format/MIME rejection worth retrying with transcoded WebP."""
    if (
        "image" not in remaining
        or not isinstance(error, InvalidRequest)
        or error.status_code == _PAYLOAD_TOO_LARGE_STATUS
        or "image" in _names_capability_gap(error, _IMAGES)
    ):
        return False
    text = str(error).lower()[:_MAX_SCANNED_ERROR_CHARS]
    if "webp" in text:
        return True
    rejected_image = any(sent.get(mid) == "image" for mid in error.rejected_media)
    if not rejected_image and "image" not in _named_media_kinds(error):
        return False
    return error.code in _WEBP_TRANSCODE_ERROR_CODES or any(marker in text for marker in _WEBP_TRANSCODE_ERROR_MARKERS)


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


def _route(inner: ModelProvider) -> _Route:
    identity = inner.identity
    return _Route(provider=identity.provider.lower(), model_id=identity.id, endpoint=identity.endpoint)


def _unsupported_kinds(inner: ModelProvider) -> frozenset[MediaKind]:
    """Return the media kinds ``inner`` is never sent: those its route omits and those it learned the model rejects."""
    return frozenset(_LEARNED_UNSUPPORTED.get(_route(inner), ())) | inner.capabilities.omitted_media


def _first_attempt(inner: ModelProvider, request: ModelRequest, *, fallback_prompt: str) -> ModelRequest:
    """Return the first attempt the media fallback sends ``inner``: answered audio and unsupported media replaced."""
    base, _present, known = _prepared_messages(inner, request.messages)
    return replace(request, messages=_without_media(base, known, frozenset(), fallback_prompt))


def _first_attempt_inputs(inner: ModelProvider, *, fallback_prompt: str) -> object:
    """Return what ``_first_attempt`` reads beside the request: the unsupported kinds and the note."""
    return [sorted(_unsupported_kinds(inner)), fallback_prompt]


def media_fallback_projection(fallback_prompt: str) -> RequestProjection:
    """Return how the media fallback rewrites a request's first attempt, which sizes and identifies the request."""
    return RequestProjection(
        partial(_first_attempt, fallback_prompt=fallback_prompt),
        partial(_first_attempt_inputs, fallback_prompt=fallback_prompt),
    )


def _sent_media(messages: Sequence[Message]) -> dict[str, MediaKind]:
    return {item.id: kind for message in messages for kind, item in media_items(message)}


async def _transcode_webp_if(should_check: bool, base: Sequence[Message]) -> tuple[Sequence[Message], bool]:
    if not should_check or (transcoded := await asyncio.to_thread(_transcode_webp_messages, base)) is None:
        return base, False
    return transcoded, True


def _narrowed_removal(
    error: ProviderError,
    base: Sequence[Message],
    sent: dict[str, MediaKind],
    known: frozenset[MediaKind],
    remaining: frozenset[MediaKind],
    fallback_prompt: str,
    log: Callable[..., Any],
) -> tuple[list[Message], frozenset[MediaKind], bool] | None:
    if not isinstance(error, InvalidRequest):
        return None
    # Only a rejected request body can name its parts; a request too large for the window shrinks as a whole.
    named = error.rejected_media
    if (selective := _selective_removal(named, sent, known)) is not None:
        removed, ids = selective
        log("Retrying model request without the inline media its failure names", rejected_media=len(ids))
        messages = _without_media(base, removed, ids, fallback_prompt)
        stripped = "image" in removed or any(sent.get(mid) == "image" for mid in ids)
        return messages, remaining - _media_kinds(messages), stripped
    kinds = _named_media_kinds(error) & remaining
    if kinds and kinds != remaining and not (named & sent.keys()):
        log("Retrying model request without the inline media kinds its failure names", media_kinds=sorted(kinds))
        return _without_media(base, known | kinds, frozenset(), fallback_prompt), kinds, "image" in kinds
    return None


async def media_fallback_stream(
    inner: ModelProvider,
    request: ModelRequest,
    *,
    fallback_prompt: str,
) -> AsyncIterator[StreamEvent]:
    """Stream ``request`` with unsupported media replaced, transcoding WebP on format rejection, then stripping."""
    identity = inner.identity
    route = _route(inner)
    base, present, known = _prepared_messages(inner, request.messages)
    remaining = present - known
    checked_webp = route in _LEARNED_WEBP_UNSUPPORTED and "image" in remaining
    base, transcoded_webp = await _transcode_webp_if(checked_webp, base)
    first = replace(request, messages=_without_media(base, known, frozenset(), fallback_prompt))
    sent = _sent_media(first.messages)
    failure: ProviderError | None = None
    sent_broad = False
    stripped_image = False
    removed_kinds: frozenset[MediaKind] = frozenset()

    async def without_rejected_media(error: ProviderError) -> Retry | None:
        nonlocal base, checked_webp, transcoded_webp, failure, sent_broad, stripped_image, removed_kinds
        if (
            request.overrides.transient_retries != "pipeline"
            or sent_broad
            or not remaining
            or error.retryable
            or error.already_retried
            or error.code in _NOT_MEDIA_RELATED_CODES
        ):
            return None
        log = partial(
            logger.warning,
            provider=identity.provider,
            model_id=identity.id,
            status_code=error.status_code,
            error=redact_sensitive_text(str(error), max_length=500),
        )
        if not checked_webp and failure is None and _is_webp_transcode_candidate(error, remaining, sent):
            checked_webp = True
            base, transcoded_webp = await _transcode_webp_if(True, base)
            if transcoded_webp:
                log("Retrying model request after transcoding WebP images")
                return Retry(replace(request, messages=_without_media(base, known, frozenset(), fallback_prompt)))
        if failure is None:
            failure = error
            if (narrowed := _narrowed_removal(error, base, sent, known, remaining, fallback_prompt, log)) is not None:
                messages, removed_kinds, narrowed_stripped = narrowed
                stripped_image = stripped_image or narrowed_stripped
                return Retry(replace(request, messages=messages))
        sent_broad = True
        removed_kinds = remaining
        stripped_image = stripped_image or "image" in remaining
        log("Retrying model request without inline media")
        return Retry(replace(request, messages=_without_media(base, present, frozenset(), fallback_prompt)))

    events = attempts(inner, first, without_rejected_media)
    async with closing_async_stream(events):
        async for event in events:
            yield event
    if transcoded_webp and not stripped_image:
        _LEARNED_WEBP_UNSUPPORTED.add(route)
    # Successful removal of a whole named kind can teach the route; partial item removal cannot.
    if failure is not None and (learned := _names_capability_gap(failure, removed_kinds)):
        _LEARNED_UNSUPPORTED.setdefault(route, set()).update(learned)
