"""The Agno document readers and chunkers knowledge indexing reads files with; the only knowledge module importing Agno."""

from __future__ import annotations

from agno.knowledge.chunking.fixed import FixedSizeChunking
from agno.knowledge.document.base import Document
from agno.knowledge.reader import ReaderFactory
from agno.knowledge.reader.base import Reader
from agno.knowledge.reader.json_reader import JSONReader
from agno.knowledge.reader.markdown_reader import MarkdownReader
from agno.knowledge.reader.text_reader import TextReader

__all__ = [
    "Document",
    "FixedSizeChunking",
    "JSONReader",
    "MarkdownReader",
    "Reader",
    "ReaderFactory",
    "TextReader",
]
