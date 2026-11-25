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
# No tokenizer or estimator packs more than 16 bytes of text per token, so any non-PDF text file larger than
# ``max_tokens * 16`` bytes is guaranteed to exceed ``max_tokens`` without reading or tokenizing it.
_MAX_TEXT_BYTES_PER_TOKEN = 16
_UTF8_CHECK_CHUNK_BYTES = 65_536


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


def _is_utf8_file(path: Path) -> bool | None:
    """Return whether ``path`` decodes as UTF-8, streaming in chunks, or None if it cannot be read."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_UTF8_CHECK_CHUNK_BYTES):
                decoder.decode(chunk)
            decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    except OSError:
        return None
    return True


def _content_exceeds_text_byte_guard(content: bytes | str, mime_type: str | None, byte_limit: int) -> bool | None:
    """Apply the text byte guard to in-memory ``content`` without decoding or tokenizing oversized text."""
    if isinstance(content, str):
        exceeds = not content.startswith("%PDF-") and (
            len(content) > byte_limit
            or (not content.isascii() and len(content) * 4 > byte_limit and len(content.encode("utf-8")) > byte_limit)
        )
        return True if exceeds else None
    if len(content) <= byte_limit or content.startswith(_PDF_MAGIC):
        return None
    if is_text_mime_type(mime_type):
        return True
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _filepath_exceeds_text_byte_guard(path: Path, mime_type: str | None, byte_limit: int) -> bool | None:
    """Apply the text byte guard to ``path`` using ``stat()`` and at most a 5-byte PDF header peek."""
    try:
        st = path.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_size <= byte_limit:
            return None
        with path.open("rb") as handle:
            header = handle.read(len(_PDF_MAGIC))
    except OSError:
        return None
    if header.startswith(_PDF_MAGIC):
        return None
    if is_text_mime_type(mime_type):
        return True
    return _is_utf8_file(path)


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


def file_tokens_within_budget(counter: TokenCounter, file: File, max_tokens: int) -> int | None:
    """Return ``file``'s token count when it fits within ``max_tokens``, or None when it exceeds that budget."""
    if max_tokens <= 0:
        return None
    byte_limit = max_tokens * _MAX_TEXT_BYTES_PER_TOKEN
    if file.content is not None:
        guarded = _content_exceeds_text_byte_guard(file.content, file.mime_type, byte_limit)
    elif file.filepath is not None:
        guarded = _filepath_exceeds_text_byte_guard(Path(file.filepath), file.mime_type, byte_limit)
    else:
        guarded = None
    if guarded is True:
        return None
    tokens = MEDIA_ALLOWANCE_TOKENS if guarded is False else _file_tokens(counter, file)
    return tokens if tokens <= max_tokens else None


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
