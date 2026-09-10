"""Native knowledge reads belong to a bounded, replaceable child process."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest
from agno.knowledge.embedder.base import Embedder
from chromadb.api.client import Client
from chromadb.config import Settings

import mindroom.knowledge.chroma_client as native_chroma
from mindroom.knowledge.indexing_config import chroma_collection_exists
from mindroom.knowledge.read_process import read_chroma
from mindroom.knowledge.read_protocol import ReadRequest
from mindroom.knowledge.read_proxy import ChromaReadProxy


@dataclass
class _Embedder(Embedder):
    def get_embedding(self, text: str) -> list[float]:
        assert text == "alpha"
        return [1.0, 0.0]


@pytest.fixture
def published_index(tmp_path: Path) -> Path:
    """A real index with independently known vectors, filters and document fields."""
    with Client(settings=Settings(is_persistent=True, persist_directory=str(tmp_path))) as client:
        collection = client.create_collection("published")
        collection.add(
            ids=["a", "b"],
            embeddings=[[1.0, 0.0], [0.0, 1.0]],
            documents=["alpha document", "beta document"],
            metadatas=[
                {"name": "Alpha", "content_id": "source-a", "team": "a"},
                {"name": "Beta", "content_id": "source-b", "team": "b"},
            ],
        )
    return tmp_path


@pytest.fixture
def capture_read_processes(monkeypatch: pytest.MonkeyPatch) -> list[subprocess.Popen[bytes]]:
    """Observe real children so completion assertions include OS resource cleanup."""
    processes: list[subprocess.Popen[bytes]] = []
    original_popen = subprocess.Popen

    def capture_process(command: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        process = original_popen(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", capture_process)
    return processes


def test_probe_does_not_open_native_client_in_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_read_processes: list[subprocess.Popen[bytes]],
) -> None:
    """A valid published collection remains readable without parent-native clients."""
    with Client(settings=Settings(is_persistent=True, persist_directory=str(tmp_path))) as client:
        client.create_collection("published")

    def reject_parent_client(*_args: object, **_kwargs: object) -> None:
        message = "Native Chroma opened in the application process"
        raise AssertionError(message)

    monkeypatch.setattr(native_chroma, "ChromaDb", reject_parent_client)
    processes = capture_read_processes
    try:
        assert chroma_collection_exists(tmp_path, "published") is True
        assert len(processes) == 1
        assert processes[0].poll() is not None, "Completed reads must release the child and its native memory"
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


@pytest.mark.asyncio
async def test_search_preserves_document_fields_and_filters(published_index: Path) -> None:
    """The process boundary must not drop IDs, vectors, scores or native filters."""
    proxy = ChromaReadProxy("published", str(published_index), _Embedder())
    documents = await proxy.async_search("alpha", limit=2, filters={"team": "a"})
    assert len(documents) == 1
    document = documents[0]
    assert (document.id, document.name, document.content, document.content_id) == (
        "a",
        "Alpha",
        "alpha document",
        "source-a",
    )
    assert document.embedding == [1.0, 0.0]
    assert document.meta_data == {"team": "a", "similarity_score": 0.0, "distances": 0.0}


@pytest.mark.asyncio
async def test_locked_database_does_not_freeze_parent_and_timeout_reaps_child(
    published_index: Path,
    capture_read_processes: list[subprocess.Popen[bytes]],
) -> None:
    """A native lock wait stays outside Python's parent GIL and has a bounded lifetime."""
    # A separate lock owner releases even if a regression holds the parent's GIL.
    lock_owner = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sqlite3,sys,time; c=sqlite3.connect(sys.argv[1]); "
        "c.execute('BEGIN EXCLUSIVE'); print('locked',flush=True); time.sleep(5); c.rollback()",
        str(published_index / "chroma.sqlite3"),
        stdout=asyncio.subprocess.PIPE,
    )
    assert lock_owner.stdout is not None
    assert await lock_owner.stdout.readline() == b"locked\n"
    processes = capture_read_processes
    processes.clear()  # The lock owner is not a read worker.
    task = asyncio.create_task(
        asyncio.to_thread(read_chroma, ReadRequest(str(published_index), "published"), timeout=1.5),
    )
    intervals: list[float] = []
    previous = time.monotonic()
    try:
        while not task.done():
            await asyncio.sleep(0.02)
            current = time.monotonic()
            intervals.append(current - previous)
            previous = current
        with pytest.raises(TimeoutError, match="Knowledge read timed out"):
            await task
        assert len(intervals) > 20
        assert max(intervals) < 0.25
        assert len(processes) == 1
        assert processes[0].poll() is not None
    finally:
        lock_owner.terminate()
        await asyncio.wait_for(lock_owner.wait(), timeout=5)
    assert await asyncio.to_thread(chroma_collection_exists, published_index, "published") is True


@pytest.mark.asyncio
async def test_embedding_failure_propagates_before_starting_child(
    published_index: Path,
    capture_read_processes: list[subprocess.Popen[bytes]],
) -> None:
    """Provider errors stay in the parent, where credential health is recorded."""

    class FailingEmbedder(Embedder):
        def get_embedding(self, text: str) -> list[float]:
            assert text == "alpha"
            message = "embedding credentials expired"
            raise PermissionError(message)

    proxy = ChromaReadProxy("published", str(published_index), FailingEmbedder())
    with pytest.raises(PermissionError, match="embedding credentials expired"):
        await proxy.async_search("alpha")
    assert capture_read_processes == []


@pytest.mark.asyncio
async def test_search_does_not_recreate_deleted_published_collection(published_index: Path) -> None:
    """A new reader must preserve disappearance instead of publishing fake-empty success."""
    proxy = ChromaReadProxy("published", str(published_index), _Embedder())
    assert await proxy.async_search("alpha")
    with Client(settings=Settings(is_persistent=True, persist_directory=str(published_index))) as client:
        client.delete_collection("published")
    with pytest.raises(RuntimeError, match="NotFoundError"):
        await proxy.async_search("alpha")
    assert await asyncio.to_thread(chroma_collection_exists, published_index, "published") is False


@pytest.mark.asyncio
async def test_native_error_logs_redacted_child_diagnostics(
    published_index: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Preserve the failing operation and native explanation without URL credentials."""
    proxy = ChromaReadProxy("published", str(published_index), _Embedder())
    with pytest.raises(RuntimeError, match="ValueError"):
        await proxy.async_search(
            "alpha",
            filters={"team": {"$invalid": "https://reader:synthetic-secret@example.test"}},
        )
    error = capfd.readouterr().err
    assert "Traceback" in error
    assert "ValueError" in error
    assert "$invalid" in error
    assert "synthetic-secret" not in error
