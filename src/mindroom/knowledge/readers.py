"""Per-file document readers and the chunking policy applied to them.

One knowledge base's chunk boundaries are decided here and nowhere else: this
module owns which Agno reader answers for a file extension, which of those
readers MindRoom reconfigures with its own chunking, and how a malformed JSON
source is replayed as text. The indexing manager consumes the readers this
module builds; it does not know the Agno reader types.
"""

from __future__ import annotations

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


#: The reader types :func:`build_reader` reconfigures with a base's own
#: chunking policy. Every decision that depends on "did MindRoom chunk this
#: reader" reads this one tuple, because a caller that disagrees with
#: ``build_reader`` about the answer chunks the same file two different ways.
_CHUNK_CONFIGURED_READER_TYPES: Final = (TextReader, MarkdownReader)


def reader_uses_configured_chunking(reader: Reader) -> bool:
    """Return whether :func:`build_reader` applies this base's chunking to ``reader``.

    Callers that re-read a file to predict the chunks indexing will embed have
    to gate on this: a reader left with its factory chunking splits the same
    file differently, so those predictions would describe chunks nothing
    inserts.
    """
    return isinstance(reader, _CHUNK_CONFIGURED_READER_TYPES)


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

    # Large markdown/plain-text files are the common source of oversized embed requests.
    if not isinstance(reader, _CHUNK_CONFIGURED_READER_TYPES):
        return reader

    return _configure_text_reader(deepcopy(reader), chunking=chunking)


def text_fallback_reader(source_text: str, *, chunking: SafeFixedSizeChunking) -> Reader:
    """Build the reader that serves the text a failed JSON parse already read."""
    return _configure_text_reader(_InMemoryTextReader(source_text), chunking=chunking)
