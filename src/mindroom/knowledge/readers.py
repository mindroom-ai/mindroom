"""Per-file document readers and the chunking policy applied to them.

One knowledge base's chunk boundaries are decided here and nowhere else: this
module owns which Agno reader answers for a file extension, which of those
readers MindRoom reconfigures with its own chunking, and how a malformed JSON
source is replayed as text. It also answers what a reader costs to re-read,
which is a question about the reader rather than about chunking. The indexing
manager consumes what this module builds and never names an Agno reader type.
"""

from __future__ import annotations

import codecs
import json
import uuid
from copy import deepcopy
from typing import IO, TYPE_CHECKING, Any, Final

from agno.knowledge.document.base import Document
from agno.knowledge.reader import ReaderFactory
from agno.knowledge.reader.json_reader import JSONReader
from agno.knowledge.reader.markdown_reader import MarkdownReader
from agno.knowledge.reader.text_reader import TextReader

from mindroom.chunking import SafeFixedSizeChunking

if TYPE_CHECKING:
    from pathlib import Path

    from agno.knowledge.reader.base import Reader

    from mindroom.config.main import Config


class MalformedJSONSourceError(Exception):
    """A JSON parser failure carrying the already-read source text."""

    def __init__(self, source_text: str, *, line: int, column: int) -> None:
        super().__init__("Malformed JSON knowledge source")
        self.source_text = source_text
        self.line = line
        self.column = column


class _FallbackAwareJSONReader(JSONReader):
    """Tag only JSON decoding failures raised inside the source reader."""

    def read(self, path: Path | IO[Any], name: str | None = None) -> list[Document]:
        try:
            return super().read(path, name=name)
        except json.JSONDecodeError as error:
            raise MalformedJSONSourceError(error.doc, line=error.lineno, column=error.colno) from error


class _InMemoryTextReader(TextReader):
    """Read the malformed JSON text already retained by its parse error.

    Only ``read`` is overridden, because indexing goes through the synchronous
    ``Knowledge.insert`` path. ``TextReader.async_read`` does not delegate to
    ``read``, so anything switching this to ``Knowledge.ainsert`` must override
    it too or it will re-read the source instead of serving the retained text.
    """

    def __init__(self, source_text: str) -> None:
        super().__init__()
        self._source_text = source_text

    def read(self, file: Path | IO[Any], name: str | None = None) -> list[Document]:
        document = Document(
            name=name or str(file),
            id=str(uuid.uuid4()),
            content=self._source_text,
        )
        if not self.chunk:
            return [document]
        return self.chunk_document(document)


def chunking_strategy_for_base(config: Config, base_id: str) -> SafeFixedSizeChunking:
    """Build the chunking strategy every text-like read of one base uses."""
    base_config = config.get_knowledge_base_config(base_id)
    return SafeFixedSizeChunking(
        chunk_size=base_config.chunk_size,
        overlap=base_config.chunk_overlap,
    )


#: Encodings whose decoded text cannot outgrow the source file's size on disk.
#: UTF-8 re-encoded to UTF-8 is itself, and stripping a BOM only shrinks it.
#: Others expand: UTF-16 holds a CJK character in two bytes where UTF-8 needs
#: three, and Latin-1 holds an accent in one where UTF-8 needs two.
_SIZE_PRESERVING_ENCODINGS: Final = frozenset({"utf-8", "utf-8-sig"})


def _decodes_within_file_size(encoding: str | None) -> bool:
    """Return whether decoding with ``encoding`` can outgrow the bytes on disk."""
    if encoding is None:
        # How TextReader and MarkdownReader spell UTF-8: both read through
        # `self.encoding or "utf-8"`.
        return True
    try:
        return codecs.lookup(encoding).name in _SIZE_PRESERVING_ENCODINGS
    except LookupError:
        # An unusable codec never reads anything; refusing it keeps the caller
        # off a path whose cost and size it cannot reason about.
        return False


def reader_rereads_within_file_size(reader: Reader) -> bool:
    """Return whether a file may be re-read through ``reader`` under a size-on-disk budget.

    Two things must hold, and the type settles only the first: the second read
    has to be cheap next to one embedding round trip (a decode is, a document
    parse or archive extraction is not), and the decoded text has to fit the
    file's size on disk, which is what makes
    :meth:`~mindroom.chunking.SafeFixedSizeChunking.max_chunk_text_bytes` --
    documented against a size on disk -- a valid budget.

    The encoding half is checked rather than inferred because no reader type
    implies it, and it is currently reachable only from code that does not
    exist yet: nothing in MindRoom passes ``encoding`` to a reader and no
    config exposes it, so today every admitted reader takes the ``None``
    branch. It is kept because the failure it prevents is silent -- adding an
    encoding option later would quietly under-budget prefetch memory rather
    than fail.

    The admitted types happen to be the ones :func:`build_reader` reconfigures.
    Do not merge the two: chunking says nothing about what a read costs, and
    the first binary format to need a chunking override would need this to keep
    refusing it.
    """
    return isinstance(reader, (TextReader, MarkdownReader)) and _decodes_within_file_size(reader.encoding)


def _configure_text_reader(
    reader: TextReader | MarkdownReader,
    *,
    chunking: SafeFixedSizeChunking,
) -> TextReader | MarkdownReader:
    """Apply one base's text chunking policy to ``reader`` in place."""
    reader.chunk = True
    reader.chunk_size = chunking.chunk_size
    reader.chunking_strategy = chunking
    return reader


def build_reader(file_path: Path, *, chunking: SafeFixedSizeChunking) -> Reader:
    """Build a per-file reader with conservative chunking for text-like content."""
    reader = ReaderFactory.get_reader_for_extension(file_path.suffix.lower())

    # ReaderFactory hands out cached shared instances, so any branch that
    # configures a reader copies it first instead of mutating the cache.
    if isinstance(reader, JSONReader):
        # Carry the factory reader's configuration (encoding, chunking) onto
        # the subclass that tags its own decode failures for the text fallback.
        return _FallbackAwareJSONReader(**deepcopy(vars(reader)))

    # Large markdown/plain-text files are the common source of oversized embed
    # requests. This decision is deliberately spelled out here rather than
    # shared with `reader_rereads_within_file_size`: the two coincide today but
    # answer different questions, and see that function for why.
    if not isinstance(reader, (TextReader, MarkdownReader)):
        return reader

    return _configure_text_reader(deepcopy(reader), chunking=chunking)


def text_fallback_reader(source_text: str, *, chunking: SafeFixedSizeChunking) -> Reader:
    """Build the reader that serves the text a failed JSON parse already read."""
    return _configure_text_reader(_InMemoryTextReader(source_text), chunking=chunking)
