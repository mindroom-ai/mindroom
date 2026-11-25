"""Size one model request the way the route that sends it renders it.

Each route maps the body its own builder renders for a request to ``RequestParts``: the text its model reads, counted
as text; the values its model reads as JSON, such as tool arguments and tool schemas, counted serialized; and what
counts by its cost instead of its characters, such as media and reasoning sent as ciphertext. Transport (ids, roles,
signatures, media bytes) counts nothing. ``route_request_tokens`` and ``route_context_key`` run a route's map, so a
route's size and its identity read one rendering.
"""

from __future__ import annotations

import codecs
import hashlib
import io
import stat
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.llm.media import File, file_text, is_text_mime_type
from mindroom.llm.messages import media_items, stable_serialize
from mindroom.native_compaction import native_replay
from mindroom.token_counting.counts import count

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import BinaryIO

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelIdentity, ModelRequest, TokenCounter

__all__ = [
    "MEDIA_ALLOWANCE_TOKENS",
    "RequestParts",
    "file_tokens_within_budget",
    "plain_request_parts",
    "route_context_key",
    "route_request_tokens",
]

# What one media item counts while its real cost is unknown: an image, audio clip, video, or file a request sends, and
# each page of a PDF on a model that does not price pages lower (``TokenCounter.pdf_page_tokens``); Claude reads a
# page as 1,500 to 3,000 tokens of text beside the page's image.
MEDIA_ALLOWANCE_TOKENS = 5_000
_PDF_MAGIC = b"%PDF-"
_UTF8_VALIDATION_CHUNK_BYTES = 65_536


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
    if file.filepath is None or not (path := Path(file.filepath)).is_file():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _pdf_pages(data: bytes) -> int:
    """Return the page count a PDF's latest page tree declares, or 1 for one it cannot read, such as a damaged file.

    The latest revision's count leaves out pages that an incremental save deleted or rewrote.
    """
    # Only a request that holds a PDF loads the parser.
    from pypdf import PdfReader  # noqa: PLC0415
    from pypdf.generic import DictionaryObject  # noqa: PLC0415

    try:
        tree = PdfReader(io.BytesIO(data)).root_object["/Pages"]
        pages = tree["/Count"] if isinstance(tree, DictionaryObject) else None
    except Exception:  # an attachment's bytes may be anything, and sizing never fails a reply
        return 1
    return max(pages, 1) if isinstance(pages, int) else 1


def _pdf_file_tokens(counter: TokenCounter, data: bytes) -> int:
    return _pdf_pages(data) * (counter.pdf_page_tokens or MEDIA_ALLOWANCE_TOKENS)


def _file_tokens(counter: TokenCounter, file: File) -> int:
    """Return what ``file`` counts: a PDF the model's page price per page, text its text, any other the allowance.

    Claude, Gemini, and OpenAI's Responses API read a text file as its text. Bytes that are not UTF-8 are text only in
    a file of a text type, such as a Windows-1252 CSV or a UTF-16 log, which counts the text Claude sends
    (``file_text``).
    """
    data = _file_bytes(file)
    if data is None:
        return MEDIA_ALLOWANCE_TOKENS
    if data.startswith(_PDF_MAGIC):
        return _pdf_file_tokens(counter, data)
    if is_text_mime_type(file.mime_type):
        return count(counter, file_text(data))
    try:
        return count(counter, data.decode("utf-8"))
    except UnicodeDecodeError:
        return MEDIA_ALLOWANCE_TOKENS


def _fits_file_budget(tokens: int, text_bytes: int, headroom_budget: int | None) -> tuple[int, int] | None:
    return (tokens, text_bytes) if headroom_budget is None or tokens <= headroom_budget else None


def _is_valid_utf8_stream(stream: BinaryIO, first: bytes) -> bool:
    """Return whether ``first`` followed by ``stream`` decodes as strict UTF-8 without buffering decoded text."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    try:
        decoder.decode(first)
        while chunk := stream.read(_UTF8_VALIDATION_CHUNK_BYTES):
            decoder.decode(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


def _read_bounded_bytes(stream: BinaryIO, header: bytes, limit: int) -> bytes:
    return header if len(header) > limit else header + stream.read(limit - len(header) + 1)


def _stream_file_tokens_within_budget(
    counter: TokenCounter,
    stream: BinaryIO,
    size_bytes: int,
    mime_type: str | None,
    *,
    text_byte_budget: int,
    headroom_budget: int | None,
) -> tuple[int, int] | None:
    """Classify and size one open byte stream against ``text_byte_budget`` and ``headroom_budget``."""
    header = stream.read(len(_PDF_MAGIC))
    if header.startswith(_PDF_MAGIC):
        return _fits_file_budget(_pdf_file_tokens(counter, header + stream.read()), 0, headroom_budget)
    if is_text_mime_type(mime_type):
        if (
            size_bytes > text_byte_budget
            or len(data := _read_bounded_bytes(stream, header, text_byte_budget)) > text_byte_budget
        ):
            return None
        return _fits_file_budget(count(counter, file_text(data)), len(data), headroom_budget)
    if size_bytes <= text_byte_budget:
        data = _read_bounded_bytes(stream, header, text_byte_budget)
        if len(data) <= text_byte_budget:
            try:
                decoded = data.decode("utf-8")
            except UnicodeDecodeError:
                return _fits_file_budget(MEDIA_ALLOWANCE_TOKENS, 0, headroom_budget)
            return _fits_file_budget(count(counter, decoded), len(data), headroom_budget)
        # File grew past ``text_byte_budget`` since ``stat()``; finish validating as an oversized stream.
        header = data
    binary_allowed = headroom_budget is None or headroom_budget >= MEDIA_ALLOWANCE_TOKENS
    return (
        _fits_file_budget(MEDIA_ALLOWANCE_TOKENS, 0, headroom_budget)
        if binary_allowed and not _is_valid_utf8_stream(stream, header)
        else None
    )


def _str_file_tokens_within_budget(
    counter: TokenCounter,
    file: File,
    content: str,
    *,
    text_byte_budget: int,
    headroom_budget: int | None,
) -> tuple[int, int] | None:
    if content.startswith("%PDF-"):
        return _fits_file_budget(_file_tokens(counter, file), 0, headroom_budget)
    if len(content) > text_byte_budget:
        return None
    size_bytes = len(content) if content.isascii() else len(content.encode("utf-8"))
    if size_bytes > text_byte_budget:
        return None
    return _fits_file_budget(_file_tokens(counter, file), size_bytes, headroom_budget)


def file_tokens_within_budget(
    counter: TokenCounter,
    file: File,
    *,
    text_byte_budget: int,
    headroom_budget: int | None,
) -> tuple[int, int] | None:
    """Return ``(tokens, text_bytes_used)`` when ``file`` fits its budgets, or None when it exceeds them.

    Text files (declared text MIME types and untyped UTF-8 files) are bounded by both ``text_byte_budget`` and
    ``headroom_budget``; PDFs and binary/missing files report ``text_bytes_used == 0`` and are bounded only by
    ``headroom_budget``.
    """
    if headroom_budget is not None and headroom_budget <= 0:
        return None
    if isinstance(file.content, str):
        return _str_file_tokens_within_budget(
            counter,
            file,
            file.content,
            text_byte_budget=text_byte_budget,
            headroom_budget=headroom_budget,
        )
    if isinstance(file.content, bytes):
        return _stream_file_tokens_within_budget(
            counter,
            io.BytesIO(file.content),
            len(file.content),
            file.mime_type,
            text_byte_budget=text_byte_budget,
            headroom_budget=headroom_budget,
        )
    if file.filepath is not None:
        path = Path(file.filepath)
        try:
            file_stat = path.stat()
            if stat.S_ISREG(file_stat.st_mode):
                with path.open("rb") as handle:
                    return _stream_file_tokens_within_budget(
                        counter,
                        handle,
                        file_stat.st_size,
                        file.mime_type,
                        text_byte_budget=text_byte_budget,
                        headroom_budget=headroom_budget,
                    )
        except OSError:
            pass
    return _fits_file_budget(MEDIA_ALLOWANCE_TOKENS, 0, headroom_budget)


def _media_tokens(counter: TokenCounter, messages: Sequence[Message], *, keep_images: bool) -> int:
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
    reset_scope: str | None = None,
) -> int:
    """Return what ``request`` gives the route's model from message ``start`` on, as ``parts_of`` renders it.

    Media are priced by ``_media_tokens``, for the messages the request replays (``native_replay``, with the route's
    ``reset_scope``): those its latest native checkpoint did not replace. ``keep_images`` leaves the images to a
    route that prices them itself.
    """
    parts = RequestParts(counter)
    route = request.native_compaction.route if request.native_compaction is not None else None
    stripped = request.strip_encrypted_replay
    sent = native_replay(request.messages[start:], route, reset_scope=reset_scope, stripped=stripped).messages
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
