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
    from collections.abc import Callable, Iterable, Iterator, Sequence
    from typing import BinaryIO

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelIdentity, ModelRequest, TokenCounter

__all__ = [
    "MEDIA_ALLOWANCE_TOKENS",
    "RequestParts",
    "file_byte_size",
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
_TEXT_STREAM_CHUNK_BYTES = 65_536
# At most a few extra tokens can appear where a chunk boundary splits a token or hits a family rounding margin.
_CHUNK_BOUNDARY_SLACK_TOKENS = 4


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
        return _pdf_pages(data) * (counter.pdf_page_tokens or MEDIA_ALLOWANCE_TOKENS)
    if is_text_mime_type(file.mime_type):
        return count(counter, file_text(data))
    try:
        return count(counter, data.decode("utf-8"))
    except UnicodeDecodeError:
        return MEDIA_ALLOWANCE_TOKENS


def file_byte_size(file: File) -> int | None:
    """Return ``file``'s size in bytes without reading its full contents from disk, or None when unavailable."""
    if isinstance(file.content, bytes):
        return len(file.content)
    if isinstance(file.content, str):
        if file.content.isascii():
            return len(file.content)
        return file.size if file.size is not None else len(file.content.encode("utf-8"))
    if file.filepath is not None:
        try:
            st = Path(file.filepath).stat()
        except OSError:
            return file.size
        if stat.S_ISREG(st.st_mode):
            return st.st_size
    return file.size


def _fits_non_text(tokens: int, headroom_budget: int | None) -> tuple[int, bool] | None:
    return (tokens, False) if headroom_budget is None or tokens <= headroom_budget else None


def _count_text_chunks_within_budget(
    counter: TokenCounter,
    text_chunks: Iterable[str],
    max_tokens: int,
) -> int | None:
    """Return exact token count when ``text_chunks`` fit ``max_tokens``, aborting early once clearly over budget."""
    if max_tokens <= 0:
        return None
    chunks: list[str] = []
    approx_tokens = 0
    for chunk in text_chunks:
        if not chunk:
            continue
        chunks.append(chunk)
        approx_tokens += count(counter, chunk)
        if approx_tokens > max_tokens + len(chunks) * _CHUNK_BOUNDARY_SLACK_TOKENS:
            return None
    exact = count(counter, "".join(chunks))
    return exact if exact <= max_tokens else None


def _read_chunks(stream: BinaryIO, first: bytes) -> Iterator[bytes]:
    if first:
        yield first
    while chunk := stream.read(_TEXT_STREAM_CHUNK_BYTES):
        yield chunk


def _decode_stream(raw_chunks: Iterable[bytes], encoding: str, *, errors: str) -> Iterator[str]:
    decoder = codecs.getincrementaldecoder(encoding)(errors=errors)
    for chunk in raw_chunks:
        yield decoder.decode(chunk)
    yield decoder.decode(b"", final=True)


def _decode_cp1252_stream(raw_seen: Sequence[bytes], stream: BinaryIO) -> Iterator[str]:
    for raw in raw_seen:
        yield raw.decode("cp1252", errors="replace")
    while raw := stream.read(_TEXT_STREAM_CHUNK_BYTES):
        yield raw.decode("cp1252", errors="replace")


def _drain_utf8_validation(decoder: codecs.IncrementalDecoder, stream: BinaryIO) -> None:
    """Validate the remaining bytes of ``stream`` as strict UTF-8 without tokenizing or buffering text."""
    while raw := stream.read(_TEXT_STREAM_CHUNK_BYTES):
        decoder.decode(raw)
    decoder.decode(b"", final=True)


def _count_text_stream_within_budget(
    counter: TokenCounter,
    stream: BinaryIO,
    first: bytes,
    max_tokens: int,
) -> int | None:
    """Count a text-MIME byte stream matching ``file_text`` semantics, aborting early once over ``max_tokens``."""
    if max_tokens <= 0:
        return None
    if first.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return _count_text_chunks_within_budget(
            counter,
            _decode_stream(_read_chunks(stream, first), "utf-16", errors="replace"),
            max_tokens,
        )
    if first.startswith(codecs.BOM_UTF8):
        return _count_text_chunks_within_budget(
            counter,
            _decode_stream(_read_chunks(stream, first), "utf-8-sig", errors="replace"),
            max_tokens,
        )
    # No BOM: try strict UTF-8 incrementally; on any non-UTF-8 byte, fall back to cp1252 from byte 0.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    raw_seen: list[bytes] = []
    chunks: list[str] = []
    approx_tokens = 0
    try:
        for raw in _read_chunks(stream, first):
            raw_seen.append(raw)
            if text := decoder.decode(raw):
                chunks.append(text)
                approx_tokens += count(counter, text)
                if approx_tokens > max_tokens + len(chunks) * _CHUNK_BOUNDARY_SLACK_TOKENS:
                    return None
        if tail := decoder.decode(b"", final=True):
            chunks.append(tail)
    except UnicodeDecodeError:
        return _count_text_chunks_within_budget(counter, _decode_cp1252_stream(raw_seen, stream), max_tokens)
    exact = count(counter, "".join(chunks))
    return exact if exact <= max_tokens else None


def _count_untyped_stream_within_budget(
    counter: TokenCounter,
    stream: BinaryIO,
    first: bytes,
    *,
    text_budget: int,
    headroom_budget: int | None,
) -> tuple[int, bool] | None:
    """Count a non-text-MIME stream as strict UTF-8 text or fall back to ``MEDIA_ALLOWANCE_TOKENS`` for binary.

    Matches ``_file_tokens``: once ``text_budget`` is exceeded, tokenization stops immediately, but when binary
    fallback still fits ``headroom_budget`` the remaining bytes are scanned for invalid UTF-8 so archives or binary
    files with a long valid-UTF-8 prefix still classify as binary.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    binary_fits = headroom_budget is None or headroom_budget >= MEDIA_ALLOWANCE_TOKENS
    try:
        if text_budget <= 0:
            if not binary_fits:
                return None
            decoder.decode(first)
            _drain_utf8_validation(decoder, stream)
            return None
        chunks: list[str] = []
        approx_tokens = 0
        for raw in _read_chunks(stream, first):
            if text := decoder.decode(raw):
                chunks.append(text)
                approx_tokens += count(counter, text)
                if approx_tokens > text_budget + len(chunks) * _CHUNK_BOUNDARY_SLACK_TOKENS:
                    if not binary_fits:
                        return None
                    chunks.clear()
                    _drain_utf8_validation(decoder, stream)
                    return None
        if tail := decoder.decode(b"", final=True):
            chunks.append(tail)
    except UnicodeDecodeError:
        return _fits_non_text(MEDIA_ALLOWANCE_TOKENS, headroom_budget)
    exact = count(counter, "".join(chunks))
    return (exact, True) if exact <= text_budget else None


def _stream_file_tokens_within_budget(
    counter: TokenCounter,
    stream: BinaryIO,
    mime_type: str | None,
    *,
    text_budget: int,
    headroom_budget: int | None,
) -> tuple[int, bool] | None:
    """Classify and count one open byte stream against text and headroom budgets."""
    header = stream.read(len(_PDF_MAGIC))
    if header.startswith(_PDF_MAGIC):
        tokens = _pdf_pages(header + stream.read()) * (counter.pdf_page_tokens or MEDIA_ALLOWANCE_TOKENS)
        return _fits_non_text(tokens, headroom_budget)
    if is_text_mime_type(mime_type):
        if text_budget <= 0:
            return None
        first = header + stream.read(_TEXT_STREAM_CHUNK_BYTES - len(header))
        tokens = _count_text_stream_within_budget(counter, stream, first, text_budget)
        return (tokens, True) if tokens is not None else None
    first = header + stream.read(_TEXT_STREAM_CHUNK_BYTES - len(header))
    return _count_untyped_stream_within_budget(
        counter,
        stream,
        first,
        text_budget=text_budget,
        headroom_budget=headroom_budget,
    )


def file_tokens_within_budget(
    counter: TokenCounter,
    file: File,
    *,
    text_budget: int,
    headroom_budget: int | None,
) -> tuple[int, bool] | None:
    """Return ``(tokens, is_text)`` when ``file`` fits its budget, or None when it exceeds it.

    Text files (text MIME types and untyped UTF-8 files) are bounded by both ``text_budget`` and ``headroom_budget``
    using chunked streaming early-exit counting. PDFs and binary/missing files are bounded only by ``headroom_budget``.
    """
    if headroom_budget is not None and headroom_budget <= 0:
        return None
    effective_text_budget = text_budget if headroom_budget is None else min(text_budget, headroom_budget)
    if isinstance(file.content, str):
        if file.content.startswith("%PDF-"):
            return _fits_non_text(_file_tokens(counter, file), headroom_budget)
        slices = (
            file.content[i : i + _TEXT_STREAM_CHUNK_BYTES]
            for i in range(0, len(file.content), _TEXT_STREAM_CHUNK_BYTES)
        )
        tokens = _count_text_chunks_within_budget(counter, slices, effective_text_budget)
        return (tokens, True) if tokens is not None else None
    if isinstance(file.content, bytes):
        return _stream_file_tokens_within_budget(
            counter,
            io.BytesIO(file.content),
            file.mime_type,
            text_budget=effective_text_budget,
            headroom_budget=headroom_budget,
        )
    if file.filepath is not None:
        path = Path(file.filepath)
        try:
            if stat.S_ISREG(path.stat().st_mode):
                with path.open("rb") as handle:
                    return _stream_file_tokens_within_budget(
                        counter,
                        handle,
                        file.mime_type,
                        text_budget=effective_text_budget,
                        headroom_budget=headroom_budget,
                    )
        except OSError:
            pass
    return _fits_non_text(MEDIA_ALLOWANCE_TOKENS, headroom_budget)


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
