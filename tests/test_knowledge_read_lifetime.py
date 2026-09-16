"""Published readers survive refresh without crossing storage or embedding scopes."""

from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from threading import Event
from typing import TYPE_CHECKING

import pytest
from agno.knowledge.embedder.base import Embedder

from mindroom.knowledge import collections, registry
from mindroom.knowledge.collections import (
    CollectionSpace,
    build_vector_db,
    candidate_collection_name,
    cleanup_superseded_collections,
)
from mindroom.knowledge.index_metadata import (
    load_published_index_state,
    save_published_index_state,
    state_for_publication,
)
from mindroom.knowledge.read_proxy import ChromaReadProxy, collection_exists
from mindroom.strict_knowledge import StrictSearchKnowledge
from tests.conftest import runtime_paths_for
from tests.knowledge_test_support import _config

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@dataclass
class _Embedder(Embedder):
    def get_embedding(self, text: str) -> list[float]:
        del text
        return [1.0, 0.0]


@pytest.fixture
def publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[CollectionSpace, registry.PublishedIndexKey, ChromaReadProxy]:
    """Build the production descriptor with one real, published native collection."""
    config = _config(tmp_path, bases={"docs": tmp_path / "docs"}, agent_bases=["docs"])
    paths = runtime_paths_for(config)
    key = registry.resolve_published_index_key("docs", config=config, runtime_paths=paths)
    space = CollectionSpace("docs", tmp_path / "docs", registry.published_index_storage_path(key), _Embedder)
    collection = _publish(space, key, "original")
    state = state_for_publication(
        settings=key.indexing_settings,
        collection=collection,
        indexed_count=1,
        source_signature="original",
        published_revision=None,
    )
    monkeypatch.setattr(registry, "create_configured_embedder", lambda *_args: _Embedder())
    proxy = registry._build_published_index_vector_db(key, state, config=config, runtime_paths=paths)
    assert isinstance(proxy, ChromaReadProxy)
    return space, key, proxy


def _publish(space: CollectionSpace, key: registry.PublishedIndexKey, content: str) -> str:
    name = candidate_collection_name(space)
    with closing(build_vector_db(space, name)) as vector_db:
        collection = vector_db.client.create_collection(name)
        collection.add(ids=[content], embeddings=[[1.0, 0.0]], documents=[content])
        save_published_index_state(
            registry.published_index_metadata_path(key),
            state_for_publication(
                settings=key.indexing_settings,
                collection=name,
                indexed_count=1,
                source_signature=content,
                published_revision=None,
            ),
        )
        cleanup_superseded_collections(space, vector_db=vector_db, preserved=frozenset({name}))
    return name


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_retained_reader_follows_refresh(
    publication: tuple[CollectionSpace, registry.PublishedIndexKey, ChromaReadProxy],
    asynchronous: bool,
) -> None:
    """An agent created before multiple swaps queries the current publication."""
    space, key, proxy = publication
    _publish(space, key, "replacement")
    _publish(space, key, "latest")
    documents = await proxy.async_search("alpha") if asynchronous else proxy.search("alpha")
    assert [document.content for document in documents] == ["latest"]


@pytest.mark.parametrize(
    "field",
    ["embedder_model", "embedder_dimensions", "knowledge_path", "storage_root", "base_id", "include_patterns"],
)
def test_retained_reader_rejects_changed_scope_or_embeddings(
    publication: tuple[CollectionSpace, registry.PublishedIndexKey, ChromaReadProxy],
    field: str,
) -> None:
    """A newer collection must not redirect the old requester or query vector."""
    space, key, proxy = publication
    name = _publish(space, key, "replacement")
    settings = replace(key.indexing_settings, **{field: "changed"})
    save_published_index_state(
        registry.published_index_metadata_path(key),
        state_for_publication(
            settings=settings,
            collection=name,
            indexed_count=1,
            source_signature="replacement",
            published_revision=None,
        ),
    )
    with pytest.raises(RuntimeError, match="Knowledge read failed \\(ValueError\\)"):
        proxy.search("alpha")


def test_retained_reader_accepts_new_chunk_settings(
    publication: tuple[CollectionSpace, registry.PublishedIndexKey, ChromaReadProxy],
) -> None:
    """Chunk boundaries do not change vector or requester compatibility."""
    space, key, proxy = publication
    _publish(space, replace(key, indexing_settings=replace(key.indexing_settings, chunk_size="2000")), "replacement")
    assert [document.content for document in proxy.search("alpha")] == ["replacement"]


@pytest.mark.parametrize("metadata", [None, "corrupt", "indexing"])
def test_retained_reader_does_not_hide_unavailable_publication(
    publication: tuple[CollectionSpace, registry.PublishedIndexKey, ChromaReadProxy],
    metadata: str | None,
) -> None:
    """Loss of the authoritative publication fails explicitly, even if old vectors exist."""
    _space, key, proxy = publication
    path = registry.published_index_metadata_path(key)
    if metadata is None:
        path.unlink()
    elif metadata == "corrupt":
        path.write_text("invalid", encoding="utf-8")
    else:
        state = load_published_index_state(path)
        assert state is not None
        save_published_index_state(path, replace(state, status="indexing"))
    with pytest.raises(RuntimeError, match="Knowledge read failed \\(ValueError\\)"):
        proxy.search("alpha")


_GATED_READER = """
import signal
import sys
from pathlib import Path
from threading import Event
from contextlib import contextmanager
from mindroom.knowledge import read_worker

release = Event()
signal.signal(signal.SIGUSR1, lambda *_: release.set())
original = read_worker.read_collection

@contextmanager
def select(request):
    with original(request) as collection:
        Path(sys.argv[1]).write_text(collection)
        release.wait()
        yield collection

read_worker.read_collection = select
read_worker._main()
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("direct_delete", [False, True])
async def test_cleanup_waits_for_selected_subprocess_and_releases_after_exit(
    publication: tuple[CollectionSpace, registry.PublishedIndexKey, ChromaReadProxy],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
    direct_delete: bool,
) -> None:
    """Selection is leased across publication, native query, and cancellation/reaping."""
    space, key, proxy = publication
    selected = tmp_path / "selected"
    processes: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def gated_spawn(*args: str, stdin: int, stdout: int, env: dict[str, str]) -> asyncio.subprocess.Process:
        process = await spawn(args[0], "-c", _GATED_READER, str(selected), stdin=stdin, stdout=stdout, env=env)
        processes.append(process)
        return process

    cleanup_started = Event()
    original_lock = collections.collection_lifetime_lock

    @contextmanager
    def observed_lock(path: Path, *, exclusive: bool) -> Iterator[None]:
        cleanup_started.set()
        with original_lock(path, exclusive=exclusive):
            yield

    monkeypatch.setattr(asyncio, "create_subprocess_exec", gated_spawn)
    monkeypatch.setattr(collections, "collection_lifetime_lock", observed_lock)
    read_task = asyncio.create_task(proxy.async_search("alpha"))
    refresh_task = None
    try:
        async with asyncio.timeout(10):
            while not selected.exists():  # noqa: ASYNC110 - Observe the real subprocess selection boundary.
                await asyncio.sleep(0.01)
        assert selected.read_text() == proxy.collection_name
        refresh_task = asyncio.create_task(
            asyncio.to_thread(collections._delete_collection_sync, space, proxy.collection_name)
            if direct_delete
            else asyncio.to_thread(_publish, space, key, "replacement"),
        )
        assert await asyncio.to_thread(cleanup_started.wait, 10)
        assert not refresh_task.done()
        assert collection_exists(str(space.storage_path), proxy.collection_name)
        if cancel:
            read_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await read_task
        else:
            processes[0].send_signal(signal.SIGUSR1)
            assert [document.content for document in await read_task] == ["original"]
        await asyncio.wait_for(refresh_task, 10)
        assert processes[0].returncode is not None
        assert not collection_exists(str(space.storage_path), proxy.collection_name)
        if direct_delete:
            await asyncio.to_thread(_publish, space, key, "replacement")
        assert [document.content for document in await asyncio.to_thread(proxy.search, "alpha")] == ["replacement"]
    finally:
        read_task.cancel()
        await asyncio.gather(read_task, return_exceptions=True)
        if refresh_task is not None:
            await asyncio.wait_for(refresh_task, 10)


def test_refresh_resolution_stays_in_requester_storage(
    publication: tuple[CollectionSpace, registry.PublishedIndexKey, ChromaReadProxy],
    tmp_path: Path,
) -> None:
    """Identical base/source identities in another requester root cannot redirect reads."""
    space, key, proxy = publication
    other_root = str(tmp_path / "another-requester")
    other_key = replace(
        key,
        storage_root=other_root,
        indexing_settings=replace(key.indexing_settings, storage_root=other_root),
    )
    other_space = replace(space, storage_path=registry.published_index_storage_path(other_key))
    _publish(space, key, "own-replacement")
    _publish(other_space, other_key, "other-private-content")
    assert [document.content for document in proxy.search("alpha")] == ["own-replacement"]


def test_knowledge_initialization_follows_refresh(
    publication: tuple[CollectionSpace, registry.PublishedIndexKey, ChromaReadProxy],
) -> None:
    """Agno's initialization probe must follow the same publication as searches."""
    space, key, proxy = publication
    _publish(space, key, "replacement")
    knowledge = StrictSearchKnowledge(vector_db=proxy)
    assert [document.content for document in knowledge.search("alpha")] == ["replacement"]


def test_registry_initialization_accepts_superseded_state_snapshot(
    publication: tuple[CollectionSpace, registry.PublishedIndexKey, ChromaReadProxy],
    tmp_path: Path,
) -> None:
    """A refresh between metadata loading and handle construction cannot hide memory."""
    space, key, _proxy = publication
    state = load_published_index_state(registry.published_index_metadata_path(key))
    assert state is not None
    _publish(space, key, "replacement")
    config = _config(tmp_path, bases={"docs": tmp_path / "docs"}, agent_bases=["docs"])
    knowledge = registry._load_queryable_index_from_state(
        key,
        state,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    assert knowledge is not None
    assert [document.content for document in knowledge.search("alpha")] == ["replacement"]


@pytest.mark.parametrize("orphan", [False, True])
def test_native_reader_deadline_releases_lock_after_parent_death(tmp_path: Path, orphan: bool) -> None:
    """A kernel deadline releases the lease even when native code holds the GIL."""
    if sys.platform != "linux":
        pytest.skip("The isolated orphan supervisor uses Linux child adoption")
    probe = tmp_path / "reader_probe.py"
    probe.write_text(
        """import ctypes, fcntl, os, signal, subprocess, sys, time
from pathlib import Path
root = Path(__file__).parent
pid_path = root / "worker.pid"
if len(sys.argv) > 1 and sys.argv[1] == "worker":
    from mindroom.knowledge import read_worker
    class BlockedNative:
        def __init__(self, **kwargs): pass
        def close(self): pass
        def search(self, **kwargs):
            pid_path.write_text(str(os.getpid()))
            ctypes.PyDLL(None).pause()
    read_worker._PublishedChromaDb = BlockedNative
    read_worker._main(timeout=float(sys.argv[2]))
elif len(sys.argv) > 1 and sys.argv[1] == "parent":
    from mindroom.knowledge import read_process
    from mindroom.knowledge.read_protocol import ReadRequest
    original = subprocess.run
    def launch(command, **kwargs):
        return original([sys.executable, __file__, "worker", command[-1]], **kwargs)
    read_process.subprocess.run = launch
    read_process.read_chroma(ReadRequest(str(root), "published", "query", [1.0]), timeout=2)
else:
    assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
    orphan = sys.argv[1] == "orphan"
    child = None
    process = subprocess.Popen(
        [sys.executable, __file__, "parent"] if orphan else
        [sys.executable, __file__, "worker", "0.3"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        if not orphan:
            process.stdin.write(('{"path":' + __import__('json').dumps(str(root)) +
                ',"collection":"published","query":"query","embedding":[1.0]}').encode())
            process.stdin.close()
        deadline = time.monotonic() + 15
        while not pid_path.exists():
            assert process.poll() is None, process.stderr.read().decode()
            assert time.monotonic() < deadline
            time.sleep(.01)
        child = int(pid_path.read_text())
        with (root / "collection_lifetime.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise AssertionError("native reader did not hold its lease")
            if orphan:
                process.kill()
                process.wait()
            deadline = time.monotonic() + 4
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    assert time.monotonic() < deadline, "orphan retained reader lease"
                    time.sleep(.01)
            if orphan:
                _, status = os.waitpid(child, 0)
                assert os.waitstatus_to_exitcode(status) == -signal.SIGALRM
            else:
                assert process.wait(timeout=2) == -signal.SIGALRM
            child = None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if child is not None and orphan:
            try:
                os.kill(child, signal.SIGKILL)
                os.waitpid(child, 0)
            except ProcessLookupError:
                pass
""",
    )
    result = subprocess.run(
        [sys.executable, str(probe), "orphan" if orphan else "direct"],
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert result.returncode == 0, result.stderr
