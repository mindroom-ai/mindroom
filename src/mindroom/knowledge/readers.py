"""Per-file document readers and the chunking policy applied to them.

One knowledge base's chunk boundaries are decided here and nowhere else: this
module owns which Agno reader answers for a file extension, which of those
readers MindRoom reconfigures with its own chunking, and how a malformed JSON
source is replayed as text. It also answers what a reader costs to re-read,
which is a question about the reader rather than about chunking. The indexing
manager consumes what this module builds and never names an Agno reader type.
"""

from __future__ import annotations

import json
import uuid
from copy import deepcopy
from typing import IO, TYPE_CHECKING, Any

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


def reader_decodes_plain_text(reader: Reader) -> bool:
    """Return whether ``reader`` decodes its source as text instead of unpacking it.

    Two independent preconditions for re-reading a file rest on this property
    and on nothing else:

    * the second read is cheap next to one embedding round trip, because it is
      a decode rather than a document parse or an archive extraction;
    * the file's size on disk bounds its decoded text, which is what makes
      :meth:`~mindroom.chunking.SafeFixedSizeChunking.max_chunk_text_bytes`
      -- documented against a size on disk -- a valid budget for it. A
      compressed container (``.docx``, ``.xlsx``, ``.pdf``) breaks that: its
      extracted text can dwarf the archive it came out of.

    This is currently the same set of types :func:`build_reader` reconfigures,
    and that is a coincidence of the readers Agno ships, not one fact. Do not
    merge the two: giving a reader MindRoom's chunking says nothing about what
    reading it costs, and the first binary format to need a chunking override
    would need this to keep answering ``False``.
    """
    return isinstance(reader, (TextReader, MarkdownReader))


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
    # shared with `reader_decodes_plain_text`: the two coincide today but
    # answer different questions, and see that function for why.
    if not isinstance(reader, (TextReader, MarkdownReader)):
        return reader

    return _configure_text_reader(deepcopy(reader), chunking=chunking)


def text_fallback_reader(source_text: str, *, chunking: SafeFixedSizeChunking) -> Reader:
    """Build the reader that serves the text a failed JSON parse already read."""
    return _configure_text_reader(_InMemoryTextReader(source_text), chunking=chunking)
