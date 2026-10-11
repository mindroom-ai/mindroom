"""Native knowledge reads belong to a bounded, replaceable child process."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from threading import Event
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest
from agno.knowledge.embedder.base import Embedder
from chromadb.api.client import Client
from chromadb.config import Settings

import mindroom.knowledge.chroma_client as native_chroma
from mindroom.knowledge import read_process
from mindroom.knowledge.indexing_config import chroma_collection_exists
from mindroom.knowledge.read_process import read_chroma
from mindroom.knowledge.read_protocol import ReadRequest
from mindroom.knowledge.read_proxy import ChromaReadProxy
from mindroom.knowledge.utils import _MultiKnowledgeVectorDb


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


def _assert_only_warm_reader_alive(processes: list[subprocess.Popen[bytes]]) -> None:
    """Every child handed a read has been reaped; only the idle replacement remains."""
    assert [process for process in processes if process.poll() is None] == [read_process._warm_reader]


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
        assert processes == [], "Existence checks must not boot a native search worker"
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_collection_probe_is_fresh_and_scoped(published_index: Path) -> None:
    """An identically named collection in another database is not our publication."""
    assert chroma_collection_exists(published_index, "published")
    with sqlite3.connect(published_index / "chroma.sqlite3") as connection:
        connection.execute("UPDATE databases SET name = 'another_database'")
    assert not chroma_collection_exists(published_index, "published")
    with sqlite3.connect(published_index / "chroma.sqlite3") as connection:
        connection.execute("UPDATE databases SET name = 'default_database', tenant_id = 'another_tenant'")
    assert not chroma_collection_exists(published_index, "published")


def test_collection_probe_does_not_create_missing_database(tmp_path: Path) -> None:
    """An absent publication remains absent without creating a directory or database."""
    missing = tmp_path / "missing"
    assert not chroma_collection_exists(missing, "published")
    assert not missing.exists()


def test_collection_probe_rejects_corrupt_metadata(tmp_path: Path) -> None:
    """A broken database is unavailable, not a falsely empty collection."""
    (tmp_path / "chroma.sqlite3").write_bytes(b"not a SQLite database")
    with pytest.raises(RuntimeError, match="Knowledge collection metadata unavailable"):
        chroma_collection_exists(tmp_path, "published")


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", [False, True])
async def test_collection_probe_waits_for_writer_and_reads_committed_metadata(
    published_index: Path,
    deleted: bool,
) -> None:
    """A brief writer must not hide memory; a committed deletion must stay missing."""
    reading = Event()

    def probe() -> bool:
        reading.set()
        return chroma_collection_exists(published_index, "published")

    connection = sqlite3.connect(published_index / "chroma.sqlite3")
    connection.execute("BEGIN EXCLUSIVE")
    if deleted:
        connection.execute("DELETE FROM collections WHERE name = 'published'")
    task = asyncio.create_task(asyncio.to_thread(probe))
    try:
        assert await asyncio.to_thread(reading.wait, 5)
        # Keep the writer active beyond the old 100 ms read deadline. Waiting
        # for the read also lets the event loop make progress during contention.
        done, _pending = await asyncio.wait({task}, timeout=0.25)
    finally:
        connection.commit()
        connection.close()
    assert await asyncio.wait_for(task, timeout=5) is (not deleted)
    assert not done, "The probe must wait for the writer before reading metadata"


def test_collection_probe_reports_locked_metadata(published_index: Path) -> None:
    """A lock is bounded and reported, never mistaken for a missing collection."""
    connection = sqlite3.connect(published_index / "chroma.sqlite3")
    try:
        connection.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="Knowledge collection metadata unavailable"):
            chroma_collection_exists(published_index, "published")
        assert time.monotonic() - started < 10
    finally:
        connection.close()
    assert chroma_collection_exists(published_index, "published")


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
async def test_merged_search_covers_more_sources_than_reader_slots(published_index: Path) -> None:
    """One query must search every assigned base despite the native child limit."""
    with Client(settings=Settings(is_persistent=True, persist_directory=str(published_index))) as client:
        for name in ("second", "third", "fourth", "fifth"):
            collection = client.create_collection(name)
            collection.add(ids=[name], embeddings=[[1.0, 0.0]], documents=[name], metadatas=[{"team": "a"}])

    merged = _MultiKnowledgeVectorDb(
        vector_dbs=[
            ChromaReadProxy(name, str(published_index), _Embedder())
            for name in ("published", "second", "third", "fourth", "fifth")
        ],
    )
    documents = await merged.async_search(query="alpha", limit=5, filters={"team": "a"})
    assert [document.id for document in documents] == ["a", "second", "third", "fourth", "fifth"]


@pytest.mark.asyncio
@pytest.mark.parametrize("async_read", [False, True])
async def test_locked_database_does_not_freeze_parent_and_timeout_reaps_child(
    published_index: Path,
    capture_read_processes: list[subprocess.Popen[bytes]],
    async_read: bool,
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

    async def prepare_request() -> ReadRequest:
        return ReadRequest(str(published_index), "published")

    task = asyncio.create_task(
        read_process.read_chroma_async(prepare_request, timeout=1.5)
        if async_read
        else asyncio.to_thread(read_chroma, ReadRequest(str(published_index), "published"), timeout=1.5),
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
        assert len(processes) == 2
        _assert_only_warm_reader_alive(processes)
    finally:
        lock_owner.terminate()
        await asyncio.wait_for(lock_owner.wait(), timeout=5)
    assert await asyncio.to_thread(chroma_collection_exists, published_index, "published") is True


@pytest.mark.asyncio
async def test_embedding_failure_reaps_started_child(
    published_index: Path,
    capture_read_processes: list[subprocess.Popen[bytes]],
) -> None:
    """Provider errors stay in the parent and clean up the child started during embedding."""

    class FailingEmbedder(Embedder):
        def get_embedding(self, text: str) -> list[float]:
            assert text == "alpha"
            message = "embedding credentials expired"
            raise PermissionError(message)

    proxy = ChromaReadProxy("published", str(published_index), FailingEmbedder())
    with pytest.raises(PermissionError, match="embedding credentials expired"):
        await proxy.async_search("alpha")
    assert len(capture_read_processes) == 2
    _assert_only_warm_reader_alive(capture_read_processes)


@pytest.mark.asyncio
async def test_async_embedding_overlaps_child_startup(
    published_index: Path,
    capture_read_processes: list[subprocess.Popen[bytes]],
) -> None:
    """A child must already be starting while the embedding request is in flight."""
    embedding_started = asyncio.Event()
    release_embedding = asyncio.Event()

    class BlockingEmbedder(Embedder):
        async def async_get_embedding(self, text: str) -> list[float]:
            assert text == "alpha"
            embedding_started.set()
            await release_embedding.wait()
            return [1.0, 0.0]

    proxy = ChromaReadProxy("published", str(published_index), BlockingEmbedder())
    task = asyncio.create_task(proxy.async_search("alpha", limit=1))
    try:
        await asyncio.wait_for(embedding_started.wait(), timeout=2)
        assert len(capture_read_processes) == 1
        assert capture_read_processes[0].poll() is None
    finally:
        release_embedding.set()
        documents = await task
    assert documents[0].id == "a"
    _assert_only_warm_reader_alive(capture_read_processes)


@pytest.mark.asyncio
async def test_cancelled_embedding_reaps_child_and_releases_capacity(
    published_index: Path,
    capture_read_processes: list[subprocess.Popen[bytes]],
) -> None:
    """Cancelled provider requests must not leave children or consume reader slots."""
    embedding_started = asyncio.Event()

    class BlockingEmbedder(Embedder):
        async def async_get_embedding(self, text: str) -> list[float]:
            assert text == "alpha"
            embedding_started.set()
            await asyncio.Event().wait()
            return [1.0, 0.0]

    for _ in range(5):
        embedding_started.clear()
        task = asyncio.create_task(
            ChromaReadProxy("published", str(published_index), BlockingEmbedder()).async_search("alpha"),
        )
        await asyncio.wait_for(embedding_started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        _assert_only_warm_reader_alive(capture_read_processes)
    assert len(capture_read_processes) == 6
    assert await ChromaReadProxy("published", str(published_index), _Embedder()).async_search("alpha")


@pytest.mark.asyncio
async def test_stalled_embedding_deadline_reaps_child(
    published_index: Path,
    capture_read_processes: list[subprocess.Popen[bytes]],
) -> None:
    """The read deadline must cover provider preparation as well as native execution."""

    async def prepare_request() -> ReadRequest:
        await asyncio.Event().wait()
        return ReadRequest(str(published_index), "published", "alpha", [1.0, 0.0])

    for _ in range(5):
        with pytest.raises(TimeoutError, match="Knowledge read timed out"):
            await read_process.read_chroma_async(prepare_request, timeout=0.05)
        _assert_only_warm_reader_alive(capture_read_processes)
    assert len(capture_read_processes) == 6


@pytest.mark.asyncio
async def test_cancelled_sync_embedder_fallback_reaps_child_before_provider_returns(
    published_index: Path,
    capture_read_processes: list[subprocess.Popen[bytes]],
) -> None:
    """A provider thread that cannot be cancelled must not retain the native child."""
    started = asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()

    class BlockingEmbedder(Embedder):
        def get_embedding(self, text: str) -> list[float]:
            assert text == "alpha"
            loop.call_soon_threadsafe(started.set)
            assert release.wait(timeout=5)
            return [1.0, 0.0]

    task = asyncio.create_task(
        ChromaReadProxy("published", str(published_index), BlockingEmbedder()).async_search("alpha"),
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(capture_read_processes) == 2
        _assert_only_warm_reader_alive(capture_read_processes)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


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


@pytest.mark.asyncio
async def test_concurrent_search_burst_preserves_results(published_index: Path) -> None:
    """More searches than reader slots still return each requested document and filter."""
    proxy = ChromaReadProxy("published", str(published_index), _Embedder())
    results = await asyncio.gather(
        *(proxy.async_search("alpha", limit=2, filters={"team": "a"}) for _ in range(8)),
        return_exceptions=True,
    )
    for documents in results:
        assert not isinstance(documents, BaseException)
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
@pytest.mark.parametrize("async_read", [False, True])
async def test_next_read_uses_warm_reader_and_replaces_it(
    published_index: Path,
    capture_read_processes: list[subprocess.Popen[bytes]],
    async_read: bool,
) -> None:
    """A read hands its request to the child the previous read left warm, then warms the next one."""
    request = ReadRequest(str(published_index), "published", "alpha", [1.0, 0.0])

    async def prepare_request() -> ReadRequest:
        return request

    async def search() -> list[str | None]:
        result = (
            await read_process.read_chroma_async(prepare_request)
            if async_read
            else await asyncio.to_thread(read_chroma, request)
        )
        return [document.id for document in result.documents]

    assert await search() == ["a", "b"]
    assert len(capture_read_processes) == 2
    _assert_only_warm_reader_alive(capture_read_processes)
    warm = read_process._warm_reader
    assert warm is not None
    assert await search() == ["a", "b"]
    assert len(capture_read_processes) == 3, "The warm child must serve the read instead of a new child"
    assert warm.returncode == 0
    _assert_only_warm_reader_alive(capture_read_processes)


def test_warm_reader_runs_outside_the_terminal_process_group() -> None:
    """Ctrl+C in the terminal reaches MindRoom, not the idle reader it keeps warm."""
    reader = read_process._spawn_reader()
    try:
        assert os.getsid(reader.pid) == reader.pid
    finally:
        reader.kill()
        reader.wait()


def test_idle_reader_exits_quietly_when_its_input_closes(capfd: pytest.CaptureFixture[str]) -> None:
    """A warm child whose parent exits or discards it leaves without a result or traceback."""
    reader = read_process._spawn_reader()
    output, _ = reader.communicate(timeout=30)
    assert (output, reader.returncode) == (b"", 0)
    assert "Traceback" not in capfd.readouterr().err


_EXIT_PROBE = """
import atexit
import sys

readers = []
# Registered before read_process imports, so it runs after the module's own exit handler.
atexit.register(lambda: print("reaped" if readers[0].returncode is not None else "alive"))

from mindroom.knowledge import read_process
from mindroom.knowledge.read_protocol import ReadRequest

read_process.read_chroma(ReadRequest(sys.argv[1], "published", "alpha", [1.0, 0.0]))
readers.append(read_process._warm_reader)
"""


def test_interpreter_exit_reaps_warm_reader(published_index: Path) -> None:
    """Application shutdown kills and reaps the idle child instead of leaving it behind."""
    result = subprocess.run(
        [sys.executable, "-c", _EXIT_PROBE, str(published_index)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert result.stdout == "reaped\n"
