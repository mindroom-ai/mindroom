"""Published metadata contention must not consume unrelated executor capacity."""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING

import pytest
from agno.knowledge.embedder.base import Embedder
from chromadb.api.client import Client
from chromadb.config import Settings

from mindroom.knowledge import registry, utils
from mindroom.knowledge.index_metadata import save_published_index_state, state_for_publication
from tests.conftest import runtime_paths_for
from tests.knowledge_test_support import _config

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass
class _Embedder(Embedder):
    def get_embedding(self, text: str) -> list[float]:
        message = f"An existence lookup must not request an embedding ({len(text)} characters)"
        raise AssertionError(message)


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
    with Client(settings=Settings(is_persistent=True, persist_directory=str(storage))) as client:
        client.create_collection("published")
    save_published_index_state(
        registry.published_index_metadata_path(key),
        state_for_publication(
            settings=key.indexing_settings,
            collection="published",
            indexed_count=1,
            source_signature="one-document",
            published_revision=None,
        ),
    )
    monkeypatch.setattr(registry, "create_configured_embedder", lambda *_args: _Embedder())
    return config, runtime_paths, storage


@pytest.mark.asyncio
async def test_locked_lookup_burst_preserves_executor_capacity_context_and_queued_cancellation(
    published_metadata: tuple[Config, RuntimePaths, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocked metadata reads stay bounded and leave unrelated work runnable."""
    config, runtime_paths, storage = published_metadata
    requester: ContextVar[str] = ContextVar("lookup_requester", default="missing")
    started: list[str] = []
    active = 0
    peak = 0
    state_lock = Lock()
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
    connection = sqlite3.connect(storage / "chroma.sqlite3")
    connection.execute("BEGIN EXCLUSIVE")
    tasks: list[asyncio.Task[utils.KnowledgeBaseAccessResolution]] = []
    with ThreadPoolExecutor(max_workers=1) as executor:
        asyncio.get_running_loop().set_default_executor(executor)
        try:
            for index in range(8):
                token = requester.set(f"request-{index}")
                try:
                    tasks.append(
                        asyncio.create_task(utils.resolve_knowledge_base_access_async("docs", config, runtime_paths)),
                    )
                finally:
                    requester.reset(token)
            await _wait_until(lambda: len(started) >= 1)
            assert await asyncio.wait_for(asyncio.to_thread(lambda: "available"), timeout=0.5) == "available"
            await _wait_until(lambda: len(started) == 4)
            for task in tasks[4:]:
                task.cancel()
            for task in tasks[4:]:
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert not any(task.done() for task in tasks[:4])
        finally:
            connection.rollback()
            connection.close()
            results = await asyncio.gather(*tasks, return_exceptions=True)

    assert peak == 4
    assert sorted(started) == ["request-0", "request-1", "request-2", "request-3"]
    for result in results[:4]:
        assert isinstance(result, utils.KnowledgeBaseAccessResolution)
        assert result.knowledge is not None
    assert all(isinstance(result, asyncio.CancelledError) for result in results[4:])
