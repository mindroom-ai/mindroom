"""The published index reader: in-process search with its own threads and a deadline."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest

import mindroom.knowledge.index_reader as index_reader_module
from mindroom.knowledge.index_reader import PublishedIndexReader
from mindroom.knowledge.vector_store import KnowledgeCollection, StoredRow

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class _Embedder:
    def __init__(self, *, stall: bool = False) -> None:
        self.stall = stall

    async def aembed(self, texts: Sequence[str], *, purpose: str) -> list[list[float]]:
        del purpose
        if self.stall:
            await asyncio.Event().wait()
        return [[1.0, 0.0] for _text in texts]


def _reader(tmp_path: Path, embedder: _Embedder) -> PublishedIndexReader:
    collection = KnowledgeCollection(path=tmp_path, name="docs")
    collection.reset()
    collection.write_source_rows(
        "a.md",
        [StoredRow(id="a1", document="alpha", metadata={"source_path": "a.md"}, embedding=[1.0, 0.0])],
    )
    return PublishedIndexReader("docs", str(tmp_path), embedder)


@pytest.mark.asyncio
async def test_search_does_not_need_the_shared_default_executor(tmp_path: Path) -> None:
    """A search still answers while every thread of the event loop's default executor is busy."""
    reader = _reader(tmp_path, _Embedder())
    release = threading.Event()
    loop = asyncio.get_running_loop()
    busy_executor = ThreadPoolExecutor(1)
    loop.set_default_executor(busy_executor)
    blocked = loop.run_in_executor(None, release.wait)
    try:
        hits = await asyncio.wait_for(reader.asearch("alpha", limit=1), timeout=5)
    finally:
        release.set()
        await blocked
        busy_executor.shutdown()

    assert [hit.content for hit in hits] == ["alpha"]


@pytest.mark.asyncio
async def test_a_stalled_embedder_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An embedder that never answers ends the search at the deadline instead of hanging prompt assembly."""
    monkeypatch.setattr(index_reader_module, "_SEARCH_TIMEOUT_SECONDS", 0.05)
    reader = _reader(tmp_path, _Embedder(stall=True))

    with pytest.raises(TimeoutError):
        await reader.asearch("alpha", limit=1)
