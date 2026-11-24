"""Slow knowledge lookups must not consume unrelated executor capacity, and none waits on a collection deletion."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from threading import Event, Lock
from typing import TYPE_CHECKING

import pytest

from mindroom.knowledge import registry, utils
from mindroom.knowledge.collection_lifetime import collection_lifetime_lock
from mindroom.knowledge.vector_store import KnowledgeCollection
from tests.conftest import runtime_paths_for
from tests.knowledge_test_support import _config, publish, seed_collection

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


class _Embedder:
    sends_batches = False

    def embed(self, texts: list[str], *, purpose: str) -> list[list[float]]:
        message = f"An existence lookup must not request a {purpose} embedding ({len(texts)} texts)"
        raise AssertionError(message)

    async def aembed(self, texts: list[str], *, purpose: str) -> list[list[float]]:
        return self.embed(texts, purpose=purpose)


async def _wait_until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(3):
        while not condition():  # noqa: ASYNC110 - Observe worker state without using its executor.
            await asyncio.sleep(0.001)


@pytest.fixture
def published_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Config, RuntimePaths, Path]:
    """Publish real metadata without involving an embedding service."""
    docs = tmp_path / "docs"
    docs.mkdir()
    config = _config(tmp_path, bases={"docs": docs}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = registry.resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    storage = registry.published_index_storage_path(key)
    seed_collection(KnowledgeCollection(path=storage, name="published"), [{"content": "one document"}])
    publish(key, collection="published", source_signature="one-document")
    monkeypatch.setattr(registry, "create_configured_embedder", lambda *_args: _Embedder())
    return config, runtime_paths, storage


@pytest.mark.asyncio
async def test_a_lookup_does_not_wait_for_a_collection_deletion(
    published_metadata: tuple[Config, RuntimePaths, Path],
) -> None:
    """A deletion's exclusive lifetime lock blocks searches of the collection, never the lookup that finds it."""
    config, runtime_paths, storage = published_metadata

    with collection_lifetime_lock(storage, exclusive=True):
        result = await asyncio.wait_for(
            utils.resolve_knowledge_base_access_async("docs", config, runtime_paths, execution_identity=None),
            timeout=3,
        )

    assert result.knowledge is not None


@pytest.mark.asyncio
async def test_blocked_lookup_burst_preserves_executor_capacity_context_and_queued_cancellation(
    published_metadata: tuple[Config, RuntimePaths, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lookups blocked on slow storage stay bounded and leave unrelated work runnable."""
    config, runtime_paths, _storage = published_metadata
    requester: ContextVar[str] = ContextVar("lookup_requester", default="missing")
    started: list[str] = []
    active = 0
    peak = 0
    state_lock = Lock()
    # Stands in for storage that stalls every lookup until it is released, such as a hung network filesystem.
    storage_released = Event()
    original_lookup = registry.get_published_index

    def lookup(
        base_id: str,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> registry.PublishedIndexResolution:
        nonlocal active, peak
        with state_lock:
            started.append(requester.get())
            active += 1
            peak = max(peak, active)
        try:
            assert storage_released.wait(timeout=5)
            return original_lookup(
                base_id,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
            )
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(utils, "get_published_index", lookup)
    tasks: list[asyncio.Task[utils.KnowledgeBaseAccessResolution]] = []
    with ThreadPoolExecutor(max_workers=1) as executor:
        asyncio.get_running_loop().set_default_executor(executor)
        try:
            for index in range(8):
                token = requester.set(f"request-{index}")
                try:
                    tasks.append(
                        asyncio.create_task(
                            utils.resolve_knowledge_base_access_async(
                                "docs",
                                config,
                                runtime_paths,
                                execution_identity=None,
                            ),
                        ),
                    )
                finally:
                    requester.reset(token)
            await _wait_until(lambda: len(started) >= 1)
            assert await asyncio.wait_for(asyncio.to_thread(lambda: "available"), timeout=0.5) == "available"
            await _wait_until(lambda: len(started) == 4)
            # An unblocked lookup finishes in milliseconds; these stay pending until the storage is released.
            await asyncio.sleep(0.1)
            for task in tasks[4:]:
                task.cancel()
            for task in tasks[4:]:
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert not any(task.done() for task in tasks[:4])
        finally:
            storage_released.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)

    assert peak == 4
    assert sorted(started) == ["request-0", "request-1", "request-2", "request-3"]
    for result in results[:4]:
        assert isinstance(result, utils.KnowledgeBaseAccessResolution)
        assert result.knowledge is not None
    assert all(isinstance(result, asyncio.CancelledError) for result in results[4:])
