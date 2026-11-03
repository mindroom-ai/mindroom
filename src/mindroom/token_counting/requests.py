"""Size one model request the way the route that sends it renders it.

Each route maps the body its own builder renders for a request to ``RequestParts``: the text its model reads, counted
as text; the values its model reads as JSON, such as tool arguments and tool schemas, counted serialized; and what
counts by its cost instead of its characters, such as media and reasoning sent as ciphertext. Transport (ids, roles,
signatures, media bytes) counts nothing. ``route_request_tokens`` and ``route_context_key`` run a route's map, so a
route's size and its identity read one rendering.
"""

from __future__ import annotations

import hashlib
import re
import zlib
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.llm.media import File
from mindroom.llm.messages import media_items, stable_serialize
from mindroom.native_compaction import native_replay_messages
from mindroom.token_counting.counts import count

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelIdentity, ModelRequest, TokenCounter

__all__ = [
    "MEDIA_ALLOWANCE_TOKENS",
    "RequestParts",
    "plain_request_parts",
    "route_context_key",
    "route_request_tokens",
]

# What one media item counts while its real cost is unknown: an image, audio clip, video, or file a request sends, and
# each page of a PDF, which Claude reads as 1,500 to 3,000 tokens of text beside the page's image, Gemini as a few
# hundred.
MEDIA_ALLOWANCE_TOKENS = 5_000
# A PDF's page object, which its page tree (``/Pages``) does not match, and the start of an object stream's data, where
# a compressed PDF keeps its page objects.
_PDF_PAGE = re.compile(rb"/Type\s*/Page(?![A-Za-z])")
_PDF_OBJECT_STREAM = re.compile(rb"/Type\s*/ObjStm\b[^>]*>>\s*stream\r?\n")
# The most a PDF's object streams inflate to while its pages are counted, so a crafted file cannot stall a count.
_MAX_INFLATED_BYTES = 1 << 24


@dataclass(slots=True)
class RequestParts:
    """What one request gives a route's model, counted with ``counter``: text, JSON values, and costs, in body order.

    ``flags`` identify settings that change how the route renders messages, for the request's identity only.
    """

    counter: TokenCounter
    pieces: list[str] = field(default_factory=list)
    tokens: int = 0
    flags: list[object] = field(default_factory=list)

    def text(self, value: str | None) -> None:
        """Count ``value`` as text the model reads."""
        if value:
            self.pieces.append(value)

    def value(self, value: object) -> None:
        """Count ``value`` as JSON the model reads, serialized."""
        if value not in (None, "", [], {}):
            self.pieces.append(stable_serialize(value))

    def cost(self, tokens: int) -> None:
        """Count ``tokens`` the provider charges for something its characters do not measure."""
        self.tokens += tokens

    def flag(self, value: object) -> None:
        """Record a setting that changes how the route renders messages."""
        self.flags.append(value)

    def counted(self) -> int:
        """Return the tokens these parts hold."""
        return count(self.counter, "".join(self.pieces)) + self.tokens


# A route's map: add what ``request`` gives its model to the parts, its messages from ``start`` on, and, from 0, the
# system prompt, tools, and settings too. The request holds no media but what the route prices itself.
type _PartsOf = Callable[["ModelRequest", int, RequestParts], None]


def _file_bytes(file: File) -> bytes | None:
    """Return a file's bytes, or None for one whose bytes are not here, such as a file given by URL or since removed."""
    if file.content is not None:
        return file.content.encode("utf-8") if isinstance(file.content, str) else file.content
    if file.filepath is None:
        return None
    try:
        return Path(file.filepath).read_bytes()
    except OSError:
        return None


def _pdf_pages(data: bytes) -> int:
    """Return how many pages a PDF holds, by its page objects and those its object streams compress, at least one."""
    view = memoryview(data)
    pages = len(_PDF_PAGE.findall(data))
    room = _MAX_INFLATED_BYTES
    for stream in _PDF_OBJECT_STREAM.finditer(data):
        if not room:
            break
        with suppress(zlib.error):
            objects = zlib.decompressobj().decompress(view[stream.end() : data.find(b"endstream", stream.end())], room)
            room -= len(objects)
            pages += len(_PDF_PAGE.findall(objects))
    return max(pages, 1)


def _file_tokens(counter: TokenCounter, file: File) -> int:
    """Return what ``file`` counts: a PDF the allowance per page, UTF-8 text its text, and any other file the allowance.

    Claude, Gemini, and OpenAI's Responses API read a text file as its text.
    """
    data = _file_bytes(file)
    if data is None:
        return MEDIA_ALLOWANCE_TOKENS
    if data.startswith(b"%PDF-"):
        return _pdf_pages(data) * MEDIA_ALLOWANCE_TOKENS
    try:
        return count(counter, data.decode("utf-8"))
    except UnicodeDecodeError:
        return MEDIA_ALLOWANCE_TOKENS


def _media_tokens(counter: TokenCounter, messages: Sequence[Message], *, keep_images: bool = False) -> int:
    """Return what the media ``messages`` carry count: a file by its bytes, any other item the allowance.

    Images are left out when the route prices them itself.
    """
    return sum(
        _file_tokens(counter, item) if isinstance(item, File) else MEDIA_ALLOWANCE_TOKENS
        for message in messages
        for kind, item in media_items(message)
        if not (keep_images and kind == "image")
    )


def _without_media(messages: Sequence[Message], *, keep_images: bool = False) -> list[Message]:
    """Return ``messages`` without the media ``_media_tokens`` prices, so a route's map renders no media bytes."""
    stripped: list[Message] = []
    for message in messages:
        updates = {
            name: None
            for name, items in (
                ("files", message.files),
                ("audio", message.audio),
                ("videos", message.videos),
                ("images", None if keep_images else message.images),
            )
            if items
        }
        stripped.append(replace(message, **updates) if updates else message)
    return stripped


def route_request_tokens(
    counter: TokenCounter,
    request: ModelRequest,
    start: int,
    parts_of: _PartsOf,
    *,
    keep_images: bool = False,
) -> int:
    """Return what ``request`` gives the route's model from message ``start`` on, as ``parts_of`` renders it.

    Media are priced by ``_media_tokens``, for the messages the request sends after its latest native checkpoint
    replaced what precedes it; ``keep_images`` leaves the images to a route that prices them itself.
    """
    parts = RequestParts(counter)
    route = request.native_compaction.route if request.native_compaction is not None else None
    sent = native_replay_messages(request.messages[start:], route)
    parts.cost(_media_tokens(counter, sent, keep_images=keep_images))
    parts_of(replace(request, messages=_without_media(request.messages, keep_images=keep_images)), start, parts)
    return parts.counted()


def route_context_key(identity: ModelIdentity, counter: TokenCounter, request: ModelRequest, parts_of: _PartsOf) -> str:
    """Identify what ``request`` gives the route's model besides its messages, as ``parts_of`` renders it.

    That is the model, its endpoint, the system prompt, tools, and settings the route renders, the flags that change
    how it renders messages, and each deferred tool's definition, which renders nothing until a tool search loads it.
    """
    parts = RequestParts(counter)
    parts_of(replace(request, messages=()), 0, parts)
    deferred = [
        [tool.name, tool.description, tool.parameters, tool.strict]
        for tool in request.tools
        if tool.name in request.deferred_tools.names
    ]
    digest = hashlib.sha256()
    for piece in (
        identity.id,
        identity.provider,
        identity.endpoint or "",
        *parts.pieces,
        stable_serialize(parts.flags),
        stable_serialize(deferred),
    ):
        digest.update(piece.encode("utf-8", "surrogatepass"))
        digest.update(b"\0")
    return digest.hexdigest()


def plain_request_parts(request: ModelRequest, start: int, parts: RequestParts) -> None:
    """Add a request as a route that sends messages by role renders it: text, tool calls, and tool definitions.

    It is the map of a route whose requests go to no provider, such as the synthetic model; a deferred tool counts
    nothing.
    """
    for message in request.messages[start:]:
        parts.value(message.content)
        parts.value(message.tool_calls)
    if not start:
        deferred = request.deferred_tools.names
        parts.value(
            [
                {key: value for key, value in asdict(tool).items() if value is not None}
                for tool in request.tools
                if tool.name not in deferred
            ],
        )
