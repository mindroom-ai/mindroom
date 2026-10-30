"""Map OpenAI SDK, OpenAI-compatible server, and transport failures to the normalized provider errors.

``retryable`` carries the transient-failure judgment the stream retry wrapper acts on; statuses and
codes are preserved so user-facing messages and compaction can still tell failures apart.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from mindroom.failure_text import failure_text
from mindroom.llm.errors import ProviderError
from mindroom.providers.errors import (
    classify_provider_error,
    media_ids,
    part_at,
    path_tokens,
    provider_failure,
    retry_after_seconds,
)
from mindroom.providers.openai.media import file_part_name

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import MediaKind

__all__ = [
    "provider_error",
    "rejected_media",
    "sdk_failures",
]

# The media kind of each part type the Chat and Responses wires send.
_PART_KINDS: dict[str, MediaKind] = {
    "image_url": "image",
    "input_image": "image",
    "input_audio": "audio",
    "file": "file",
    "input_file": "file",
}
# How OpenAI names a rejected image whose ``param`` names no part. Recorded in October 2026: Chat's unreadable image
# ("You uploaded an unsupported image. ...", ``invalid_image_format``) and its image URL it could not download ("Failed
# to download file.", ``invalid_image_url``), and Responses' unreadable image ("The image data you provided does not
# represent a valid image. ...", ``invalid_value`` with ``param: "input"``). The other codes are reported for images
# OpenAI could not parse.
_IMAGE_CODES = frozenset({"invalid_image_format", "invalid_image_url", "image_parse_error", "invalid_image"})
_IMAGE_WORDING = ("does not represent a valid image",)
# How OpenAI names a rejected file whose ``param`` names no part: Azure's ``unsupported_file`` code, Responses'
# unreadable file, recorded in October 2026 without a code or ``param`` ("You uploaded an invalid file. Please try again
# with a different file"), and its reported "The file type you uploaded is not supported. ...".
_FILE_CODES = frozenset({"unsupported_file"})
_FILE_WORDING = ("you uploaded an invalid file", "the file type you uploaded is not supported")
# Responses' reported rejection of a file by its extension: "Expected file type to be a supported format: .pdf but got
# .md."
_REJECTED_EXTENSION = re.compile(r"but got (\.[a-z0-9]{1,16})(?![a-z0-9])")


def sdk_failures() -> type[Exception]:
    """Return the exception type adapters map with ``provider_error``; resolved late to keep the SDK unloaded.

    The SDK raises every failure as its own error, a transport failure in a request or a stream included.
    """
    import openai  # noqa: PLC0415 - keep the SDK out of module import time

    return openai.OpenAIError


def _error_fields(body: object) -> tuple[str | None, str | None, str | None]:
    """Return the message, code, and type of an error body the SDK already unwrapped from its ``error`` envelope."""
    if not isinstance(body, Mapping):
        return (body if isinstance(body, str) and body else None), None, None
    fields = cast("Mapping[str, Any]", body)
    message, code, error_type = fields.get("message"), fields.get("code"), fields.get("type")
    return (
        message if isinstance(message, str) else None,
        str(code) if code is not None else None,
        error_type if isinstance(error_type, str) else None,
    )


def provider_error(error: Exception, *, provider: str) -> ProviderError:
    """Return the normalized error for one SDK or transport failure; ``provider`` is the metrics provider name."""
    import openai  # noqa: PLC0415 - keep the SDK out of module import time

    if (failure := provider_failure(error, provider=provider)) is not None:
        return failure
    if isinstance(error, openai.APIStatusError):
        message, code, error_type = _error_fields(error.body)
        if message is None:
            message = error.response.text or str(error)
        return classify_provider_error(
            message,
            provider=provider,
            status_code=error.status_code,
            code=code,
            error_type=error_type,
            retry_after_seconds=retry_after_seconds(error.response.headers),
        )
    if isinstance(error, openai.APIError):
        # The SDK raises an error chunk inside a stream as an ``APIError`` that carries the chunk's ``error`` body.
        message, code, error_type = _error_fields(error.body)
        return classify_provider_error(message or error.message, provider=provider, code=code, error_type=error_type)
    return ProviderError(failure_text(error), provider=provider)


def rejected_media(  # noqa: PLR0911 - one return per rule
    error: Exception,
    body: Mapping[str, Any],
    messages: Sequence[Message],
    *,
    chat: bool,
) -> frozenset[str]:
    """Return the ids of the attachments an SDK rejection names, among ``messages``, the ones the request ``body`` sent.

    ``chat`` says the body is a Chat Completions request, else a Responses one. The first rule that matches decides:

    1. A ``param`` path to a media part names its kind, as for a model without image input, or for a file its type:
       the files the body sent under the part's data-URL type, or the file at the part's URL. A path that ends at a file
       part's ``type`` names every file, as for a model without file input.
    2. A download failure that quotes a URL names the media at that URL.
    3. Responses' "but got .md" names the files the body sent under a name with that extension.
    4. An image code or wording names every image, and a file code or wording every file.

    Anything else, an error without a body included, names none.
    """
    import openai  # noqa: PLC0415 - keep the SDK out of module import time

    if not isinstance(error, openai.APIError):
        return frozenset()
    text, code, _error_type = _error_fields(error.body)
    lowered = (text or "").casefold()
    if isinstance(error.param, str):
        tokens = path_tokens(error.param)
        part = part_at(body, tokens)
        if part is not None and (kind := _PART_KINDS.get(part["type"])) is not None:
            if kind == "file" and tokens[-1:] != ["type"]:
                return _files_like(part, body, messages, chat=chat)
            return media_ids(messages, kind)
    if text and "download" in lowered and (at_url := _media_at_urls(text, messages)):
        return at_url
    if (extension := _REJECTED_EXTENSION.search(lowered)) is not None:
        names = {name for name, _type in _sent_files(body) if name.casefold().endswith(extension[1])}
        return _files_named(names, messages, chat=chat)
    if code in _IMAGE_CODES or any(wording in lowered for wording in _IMAGE_WORDING):
        return media_ids(messages, "image")
    if code in _FILE_CODES or any(wording in lowered for wording in _FILE_WORDING):
        return media_ids(messages, "file")
    return frozenset()


def _file_fields(part: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the fields of a file part: Chat nests them under ``file``, Responses keeps them on the part."""
    fields = part.get("file") if part["type"] == "file" else part
    return fields if isinstance(fields, Mapping) else {}


def _data_url_type(value: object) -> str | None:
    """Return the MIME type a ``data:`` URL declares, or None for any other value."""
    if not isinstance(value, str) or not value.startswith("data:"):
        return None
    return value.removeprefix("data:").partition(";")[0].casefold() or None


def _sent_files(body: Mapping[str, Any]) -> Iterator[tuple[str, str | None]]:
    """Yield the name and data-URL type of each inline file part the body sent."""
    for item in (*_as_list(body.get("messages")), *_as_list(body.get("input"))):
        content = item.get("content") if isinstance(item, Mapping) else None
        for part in _as_list(content):
            if not isinstance(part, Mapping) or part.get("type") not in {"file", "input_file"}:
                continue
            fields = _file_fields(part)
            if isinstance(name := fields.get("filename"), str):
                yield name, _data_url_type(fields.get("file_data"))


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _files_like(
    part: Mapping[str, Any],
    body: Mapping[str, Any],
    messages: Sequence[Message],
    *,
    chat: bool,
) -> frozenset[str]:
    """Return the files a rejected file part stands for: the file at its URL, else every file sent with its type."""
    fields = _file_fields(part)
    if isinstance(url := fields.get("file_url"), str):
        return frozenset(file.id for message in messages for file in message.files or () if file.url == url)
    if (rejected_type := _data_url_type(fields.get("file_data"))) is None:
        return media_ids(messages, "file")
    names = {name for name, sent_type in _sent_files(body) if sent_type == rejected_type}
    return _files_named(names, messages, chat=chat)


def _files_named(names: set[str], messages: Sequence[Message], *, chat: bool) -> frozenset[str]:
    """Return the ids of the files whose parts carry one of ``names`` (``file_part_name``)."""
    return frozenset(
        file.id for message in messages for file in message.files or () if file_part_name(file, chat=chat) in names
    )


def _media_at_urls(text: str, messages: Sequence[Message]) -> frozenset[str]:
    """Return the ids of the images and files sent by URL whose URL ``text`` quotes."""
    return frozenset(
        item.id
        for message in messages
        for item in (*(message.images or ()), *(message.files or ()))
        if item.url and item.url in text
    )
